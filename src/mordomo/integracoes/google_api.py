"""Cliente REST do Google — o ÚNICO módulo que fala com o Google.

Mesma decisão do WhatsApp (ADR-009), pelo mesmo motivo: a superfície que
usamos é minúscula (trocar code, renovar token, criar UM evento) e o SDK
oficial (`google-api-python-client` + `google-auth`) traz dezenas de
megabytes, descoberta dinâmica de API e um modelo de credencial próprio para
resolver um problema que aqui são três POSTs. Com httpx — que já é
dependência — o resto do piloto continua lógica pura, testável sem rede.

Referências:
  https://developers.google.com/identity/protocols/oauth2/web-server
  https://developers.google.com/calendar/api/v3/reference/events/insert
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from ..config import settings

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)

URL_TOKEN = "https://oauth2.googleapis.com/token"
URL_CALENDARIO = "https://www.googleapis.com/calendar/v3/calendars"

# O piloto escreve SEMPRE no calendário principal. Não listamos calendários:
# seria mais um escopo, mais uma chamada e uma escolha a fazer na conversa —
# tudo isso é fase 2 (ADR-010).
CALENDARIO = "primary"

# 4xx que NÃO significam "esse consentimento não vale mais": pedir de novo mais
# tarde resolve. 429 é o limite de uso do Google; 408, timeout da requisição.
STATUS_TRANSITORIOS = frozenset({408, 429})

# Tetos da leitura paginada. `POR_PAGINA` é o que cabe numa resposta do Google
# (o máximo dele é 2500; 250 é o padrão e mantém a resposta pequena).
# `TETO_EVENTOS` é o teto TOTAL de uma consulta e `MAX_PAGINAS` é o cinto de
# segurança contra laço: página vazia com nextPageToken é resposta legal.
POR_PAGINA = 250
TETO_EVENTOS = 250
MAX_PAGINAS = 20

# Razões de 403 que são limite de uso, não permissão. O Google devolve 403 nos
# dois casos e só o `reason` distingue — tratar quota como permissão negada
# apagaria a credencial da família num pico de tráfego.
# https://developers.google.com/calendar/api/guides/errors
RAZOES_DE_LIMITE = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}
)


class GoogleErro(RuntimeError):
    """Erro do Google já traduzido para um motivo CATEGÓRICO.

    `motivo` é o que vai para analytics e para o texto do usuário; a mensagem
    completa fica só no log/exceção, para quem investiga. Nunca carrega token
    nem code."""

    def __init__(self, motivo: str, status: int | None = None, detalhe: str = "") -> None:
        self.motivo = motivo
        self.status = status
        super().__init__(f"google:{motivo} status={status} {detalhe}"[:300])

    @property
    def permanente(self) -> bool:
        """4xx = "esse consentimento não vale mais"; o resto é transitório.

        Quem chama usa isto para decidir se APAGA a credencial do membro. Rede
        fora (status None), 5xx e resposta estranha NÃO podem apagar nada: um
        blip do Google obrigaria a família inteira a reautorizar. 408 e 429 são
        4xx pela letra, mas dizem "volte depois" — entram na mesma proteção."""
        if self.status is None or self.status in STATUS_TRANSITORIOS:
            return False
        return 400 <= self.status < 500


class GoogleAPI:
    """Chamadas HTTP do Google. `cliente` é injetável para os testes rodarem
    contra httpx.MockTransport — sem rede, sem chave (regra nº 7)."""

    def __init__(self, cliente: httpx.AsyncClient | None = None) -> None:
        self._cliente = cliente
        self._proprio = cliente is None

    @property
    def cliente(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(timeout=TIMEOUT)
        return self._cliente

    async def fechar(self) -> None:
        if self._cliente is not None and self._proprio:
            await self._cliente.aclose()
            self._cliente = None

    # ── OAuth ────────────────────────────────────────────────────────────

    async def _token(self, dados: dict, motivo_falha: str) -> dict:
        """POST no endpoint de token. O client_secret vai no CORPO, nunca na
        URL: query string vaza para log de proxy, de servidor e de browser."""
        try:
            resposta = await self.cliente.post(URL_TOKEN, data=dados)
        except httpx.HTTPError as erro:
            raise GoogleErro(motivo_falha, detalhe=type(erro).__name__) from erro
        if resposta.status_code >= 400:
            # `_resumo` corta e não loga o corpo inteiro — resposta de token
            # pode conter credencial em caso de erro parcial.
            raise GoogleErro(motivo_falha, resposta.status_code, _resumo(resposta))
        dados_json = resposta.json()
        if not dados_json.get("access_token"):
            raise GoogleErro(motivo_falha, resposta.status_code, "sem access_token")
        return dados_json

    async def trocar_code(self, code: str) -> dict:
        return await self._token(
            {
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            "troca_falhou",
        )

    async def renovar(self, refresh_token: str) -> dict:
        """Refresh de rotina. A resposta normalmente NÃO traz refresh_token —
        quem chama tem que preservar o antigo (ver google.salvar_conexao)."""
        return await self._token(
            {
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            },
            "refresh_falhou",
        )

    # ── Calendar ─────────────────────────────────────────────────────────

    async def criar_evento(
        self, access_token: str, evento: dict, *, com_conferencia: bool = False
    ) -> dict:
        """POST .../calendars/primary/events — devolve o evento criado.

        O `id` vai DENTRO do corpo: é ele que dá idempotência (o Google
        responde 409 se já existir um evento com aquele id no calendário).
        409 sobe como GoogleErro('evento_duplicado'), que quem chama trata
        como sucesso — não como falha.

        `com_conferencia` liga `conferenceDataVersion=1`. Sem esse parâmetro de
        QUERY o Google IGNORA o `conferenceData` do corpo e devolve 200 com um
        evento sem Meet — foi assim que um "criei o link da videochamada" virou
        um evento sem link nenhum (17/08/2026)."""
        url = f"{URL_CALENDARIO}/{CALENDARIO}/events"
        parametros = {"conferenceDataVersion": "1"} if com_conferencia else None
        try:
            resposta = await self.cliente.post(
                url,
                json=evento,
                params=parametros,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as erro:
            raise GoogleErro("rede_indisponivel", detalhe=type(erro).__name__) from erro
        if resposta.status_code == 409:
            raise GoogleErro("evento_duplicado", 409)
        _conferir(resposta)
        return resposta.json()

    async def listar_eventos(
        self,
        access_token: str,
        *,
        inicio: datetime,
        fim: datetime,
        texto: str | None = None,
        teto: int = TETO_EVENTOS,
    ) -> dict:
        """GET .../calendars/primary/events na janela [inicio, fim), PAGINANDO.

        Devolve {"itens", "truncado"}. `truncado=True` significa "a janela não
        acabou, eu parei no teto" — e quem responde ao usuário tem que dizer
        isso. Tratar página parcial como busca completa é o que fazia o Mordomo
        responder "não encontrei" sem ter procurado (conversa real, 17/08/2026).

        `singleEvents=true` expande recorrência em ocorrências — sem ele um
        evento semanal volta UMA vez, como regra de repetição, e o dia de hoje
        aparece vazio. `orderBy=startTime` só é aceito junto com ele.

        `maxResults` é teto de PÁGINA (o Google nunca devolve mais que isso de
        uma vez); quem limita o total é `teto`, seguindo `nextPageToken` até a
        janela terminar ou o teto encher.

        `texto` vai como `q` — a busca oficial do Calendar, feita no servidor:
        sem ela, achar uma reunião pelo nome exigiria paginar a agenda toda."""
        url = f"{URL_CALENDARIO}/{CALENDARIO}/events"
        parametros = {
            "timeMin": _rfc3339(inicio),
            "timeMax": _rfc3339(fim),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if texto:
            parametros["q"] = texto

        itens: list[dict] = []
        pagina: str | None = None
        # Teto de páginas além do teto de itens: página vazia com nextPageToken
        # é resposta legal do Google (eventos filtrados), e sem este limite uma
        # sequência dessas viraria laço.
        for _ in range(MAX_PAGINAS):
            restante = teto - len(itens)
            if restante <= 0:
                return {"itens": itens, "truncado": True}
            consulta = dict(parametros, maxResults=str(min(restante, POR_PAGINA)))
            if pagina:
                consulta["pageToken"] = pagina
            corpo = await self._pagina_de_eventos(url, consulta, access_token)
            recebidos = corpo.get("items")
            # Cortamos no teto aqui, sem confiar no `maxResults`: ele é um
            # PEDIDO, e uma página maior que o combinado não pode furar o teto.
            itens.extend((recebidos if isinstance(recebidos, list) else [])[:restante])
            pagina = corpo.get("nextPageToken")
            if not pagina:
                # Sem token seguinte a janela ACABOU — mesmo que o teto tenha
                # sido alcançado no exato último item.
                return {"itens": itens[:teto], "truncado": False}
        return {"itens": itens[:teto], "truncado": True}

    async def _pagina_de_eventos(self, url: str, consulta: dict, access_token: str) -> dict:
        try:
            resposta = await self.cliente.get(
                url, params=consulta, headers={"Authorization": f"Bearer {access_token}"}
            )
        except httpx.HTTPError as erro:
            raise GoogleErro("rede_indisponivel", detalhe=type(erro).__name__) from erro
        _conferir(resposta)
        corpo = resposta.json()
        return corpo if isinstance(corpo, dict) else {}


def _rfc3339(quando: datetime) -> str:
    """O Calendar exige RFC3339 com fuso; mandamos sempre em UTC (`Z`) para não
    depender de como o calendário da pessoa está configurado."""
    return quando.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _conferir(resposta: httpx.Response) -> None:
    """Traduz o status da chamada de calendário em GoogleErro categórico."""
    if resposta.status_code == 401:
        # Token expirado/revogado — quem chama decide renovar e repetir.
        raise GoogleErro("token_expirado", 401)
    if resposta.status_code == 403:
        # Só permissão de verdade é definitiva: quota estourada é "tente
        # mais tarde" e não pode custar a credencial de ninguém.
        if _razoes(resposta) & RAZOES_DE_LIMITE:
            raise GoogleErro("rede_indisponivel", 403, _resumo(resposta))
        raise GoogleErro("permissao_negada", 403, _resumo(resposta))
    if resposta.status_code >= 400:
        raise GoogleErro("calendario_recusou", resposta.status_code, _resumo(resposta))


def _razoes(resposta: httpx.Response) -> set[str]:
    """As `reason` do corpo de erro do Google (`error.errors[].reason`).

    Corpo estranho (HTML de proxy, erro sem detalhe) vira conjunto vazio — sem
    razão conhecida, quem decide é o status HTTP."""
    try:
        erro = resposta.json().get("error")
    except Exception:  # noqa: BLE001 — corpo ilegível é "não sei a razão"
        return set()
    if not isinstance(erro, dict):
        return set()
    return {
        item["reason"]
        for item in erro.get("errors") or []
        if isinstance(item, dict) and isinstance(item.get("reason"), str)
    }


def _resumo(resposta: httpx.Response) -> str:
    """O Google às vezes devolve HTML (proxy, manutenção); json() explodiria
    por cima do erro que a gente quer LER. Cortado curto de propósito."""
    try:
        erro = resposta.json().get("error")
    except Exception:  # noqa: BLE001 — ler o erro importa mais que o tipo dele
        return resposta.text[:120]
    if isinstance(erro, dict):
        return str(erro.get("message") or erro.get("status") or "")[:120]
    return str(erro)[:120]
