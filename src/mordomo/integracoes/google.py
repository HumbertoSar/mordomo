"""Piloto Google Calendar — a LÓGICA (sem rede, sem FastAPI, sem canal).

O que mora aqui: disponibilidade da integração, guarda cifrada da conexão de
cada membro, ciclo de vida do `state` do OAuth e as respostas em texto dos
comandos. Quem fala HTTP com o Google é `google_api.py`; quem expõe a rota do
callback é `google_callback.py`; quem entrega o texto à família é
`channels/comandos.py`. Ver docs/adr/010-google-calendar-piloto.md.

Regras que este módulo existe para não deixar quebrar:
  - a vinculação member_id ↔ conta Google vem do `state` PERSISTIDO, nunca de
    parâmetro enviado pelo navegador;
  - token nunca sai daqui em claro: nem para log, nem para analytics, nem para
    o LLM (ADR-005);
  - integração desligada ou quebrada nunca vira traceback na conversa.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from ..analytics import emitir
from ..config import settings
from ..db.models import GoogleConnection, OAuthState
from ..db.session import Sessao
from .cripto import CriptoErro, cifrar, decifrar
from .google_api import GoogleAPI, GoogleErro

log = logging.getLogger(__name__)

PROVEDOR = "google"

# Escopo MÍNIMO: criar/ler eventos. Não pedimos calendar (que dá poder sobre
# calendários inteiros), nem e-mail/perfil — o piloto não precisa saber quem é
# a conta, e não pedir é a forma mais barata de não vazar.
ESCOPO = "https://www.googleapis.com/auth/calendar.events"

URL_CONSENTIMENTO = "https://accounts.google.com/o/oauth2/v2/auth"

# O state é ida-e-volta de navegador: minutos, não horas. Curto encolhe a
# janela de replay; 15 min sobram para quem foi buscar a senha.
VALIDADE_STATE_MINUTOS = 15


# ── Disponibilidade ──────────────────────────────────────────────────────


def disponivel() -> bool:
    return settings.google_habilitado


# ── Conexão do membro (tokens cifrados em repouso) ───────────────────────


async def conexao_de(member_id: int) -> GoogleConnection | None:
    async with Sessao() as s:
        res = await s.execute(
            select(GoogleConnection).where(GoogleConnection.member_id == member_id)
        )
        return res.scalar_one_or_none()


def _decifrar_ou_none(cifrado: str | None) -> str | None:
    """Token ilegível (chave rotacionada, dado alterado) NÃO é exceção aqui:
    é 'não tenho credencial' — e a resposta certa ao membro é reconectar."""
    if not cifrado:
        return None
    try:
        return decifrar(cifrado, settings.google_token_key)
    except CriptoErro:
        log.warning("Token do Google ilegível (chave trocada?) — pedindo reconexão")
        return None


def access_token_de(conexao: GoogleConnection | None) -> str | None:
    return _decifrar_ou_none(conexao.access_token_cifrado) if conexao else None


def refresh_token_de(conexao: GoogleConnection | None) -> str | None:
    return _decifrar_ou_none(conexao.refresh_token_cifrado) if conexao else None


def credencial_utilizavel(conexao: GoogleConnection | None) -> bool:
    """Existe linha E existe pelo menos um token LEGÍVEL nela.

    Depois de rotacionar GOOGLE_TOKEN_KEY a linha continua lá, mas nada dentro
    dela abre: tratar isso como "conectado" prende a pessoa num beco — /google
    diz "já conectado" e /google_teste diz "mande /google"."""
    return bool(access_token_de(conexao) or refresh_token_de(conexao))


async def salvar_conexao(
    member_id: int,
    *,
    access_token: str,
    refresh_token: str | None = None,
    expira_em: datetime | None = None,
    escopo: str = "",
) -> None:
    """Cria ou atualiza a conexão do membro (um membro, uma conexão).

    `escopo` é o que o Google DE FATO concedeu — quem chama passa o valor
    da resposta, e o padrão vazio existe para nunca inventarmos permissão.

    `refresh_token=None` PRESERVA o que já existe: o Google só devolve
    refresh_token no primeiro consentimento, e um refresh de rotina traz
    apenas access_token. Sobrescrever com None mataria a conexão de forma
    silenciosa — o erro só apareceria uma hora depois, no próximo refresh."""
    access_cifrado = cifrar(access_token, settings.google_token_key)
    refresh_cifrado = (
        cifrar(refresh_token, settings.google_token_key) if refresh_token else None
    )
    async with Sessao() as s:
        res = await s.execute(
            select(GoogleConnection).where(GoogleConnection.member_id == member_id)
        )
        conexao = res.scalar_one_or_none()
        if conexao is None:
            s.add(
                GoogleConnection(
                    member_id=member_id,
                    access_token_cifrado=access_cifrado,
                    refresh_token_cifrado=refresh_cifrado,
                    expira_em=expira_em,
                    escopo=escopo,
                )
            )
            try:
                await s.commit()
                return
            except IntegrityError:
                # Corrida: dois callbacks do mesmo membro (duplo clique no
                # botão do Google). O UNIQUE(member_id) derruba o segundo —
                # que então segue pelo caminho de UPDATE, abaixo.
                await s.rollback()
                res = await s.execute(
                    select(GoogleConnection).where(GoogleConnection.member_id == member_id)
                )
                conexao = res.scalar_one()
        conexao.access_token_cifrado = access_cifrado
        if refresh_cifrado is not None:
            conexao.refresh_token_cifrado = refresh_cifrado
        conexao.expira_em = expira_em
        conexao.escopo = escopo
        await s.commit()


async def _guardar_teste(
    member_id: int, event_id: str, link: str | None, quando: datetime
) -> str | None:
    """Grava o registro local do evento de teste e devolve o link VIGENTE.

    `link=None` não apaga o que já está lá: dois /google_teste simultâneos se
    cruzam (um cria e guarda o link, o outro toma 409 com um snapshot antigo em
    mãos) e escrever o None do snapshot velho por cima destruiria o único link
    que temos. Por isso a coluna só entra no UPDATE quando há link, e o valor
    devolvido é relido do banco — não é o do snapshot de quem chamou."""
    async with Sessao() as s:
        valores: dict = {"teste_event_id": event_id, "teste_criado_em": quando}
        if link is not None:
            valores["teste_link"] = link
        await s.execute(
            update(GoogleConnection)
            .where(GoogleConnection.member_id == member_id)
            .values(**valores)
        )
        await s.commit()
        res = await s.execute(
            select(GoogleConnection.teste_link).where(
                GoogleConnection.member_id == member_id
            )
        )
        return res.scalar_one_or_none()


async def desconectar(member_id: int) -> bool:
    """Apaga a conexão local. Devolve False quando não havia nada (idempotente).

    NÃO chamamos o endpoint de revogação do Google nesta fatia — decisão
    consciente, registrada na ADR-010: revogar exige mais um caminho de rede
    que pode falhar e travar a saída do usuário. Quem quiser cortar de vez
    remove o acesso em myaccount.google.com/permissions, e o texto do comando
    diz isso."""
    async with Sessao() as s:
        res = await s.execute(
            delete(GoogleConnection).where(GoogleConnection.member_id == member_id)
        )
        await s.commit()
        return res.rowcount > 0


# ── state do OAuth (CSRF + replay + vinculação ao membro) ────────────────


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


async def criar_state(member_id: int) -> str:
    """Devolve o state em claro (vai na URL) e guarda só o hash."""
    state = secrets.token_urlsafe(32)
    async with Sessao() as s:
        s.add(
            OAuthState(
                provedor=PROVEDOR,
                state_hash=_hash(state),
                member_id=member_id,
                expira_em=datetime.now(UTC) + timedelta(minutes=VALIDADE_STATE_MINUTOS),
            )
        )
        await s.commit()
    return state


async def consumir_state(state: str) -> tuple[int | None, str]:
    """Valida e QUEIMA o state. Devolve (member_id, motivo).

    Motivos: ok · state_invalido · state_usado · state_expirado. O consumo é
    um UPDATE condicional (mesma técnica do /vincular): dois callbacks
    simultâneos com o mesmo state — retry do navegador — só têm um vencedor."""
    if not state:
        return None, "state_invalido"
    async with Sessao() as s:
        res = await s.execute(
            select(OAuthState).where(OAuthState.state_hash == _hash(state))
        )
        registro = res.scalar_one_or_none()
        if registro is None:
            return None, "state_invalido"
        if registro.usado_em is not None:
            return None, "state_usado"
        if registro.expira_em < datetime.now(UTC):
            return None, "state_expirado"
        queimado = await s.execute(
            update(OAuthState)
            .where(OAuthState.id == registro.id, OAuthState.usado_em.is_(None))
            .values(usado_em=datetime.now(UTC))
        )
        await s.commit()
        if queimado.rowcount != 1:
            return None, "state_usado"
        return registro.member_id, "ok"


def url_de_consentimento(state: str) -> str:
    """URL do consentimento do Google.

    `access_type=offline` + `prompt=consent` são o que garante refresh_token:
    sem eles, o segundo consentimento do mesmo usuário volta SEM refresh e a
    conexão morre em uma hora."""
    return f"{URL_CONSENTIMENTO}?" + urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": ESCOPO,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
        }
    )


async def iniciar_conexao(member_id: int) -> str | None:
    """Cria o state e devolve a URL. None = integração indisponível."""
    if not disponivel():
        return None
    state = await criar_state(member_id)
    await emitir("google_connection_started", member_id)
    return url_de_consentimento(state)


# ── callback (a volta do navegador) ──────────────────────────────────────


async def processar_callback(
    code: str | None,
    state: str | None,
    erro: str | None = None,
    api: GoogleAPI | None = None,
) -> tuple[bool, str]:
    """Troca o `code` por tokens e conecta o membro DO STATE.

    Devolve (sucesso, motivo). Motivos de recusa, todos categóricos:
      indisponivel · negado_pelo_usuario · state_invalido · state_usado ·
      state_expirado · sem_code · troca_falhou · escopo_insuficiente

    ORDEM IMPORTA: o state é validado ANTES de qualquer chamada ao Google.
    Trocar o code primeiro entregaria a um atacante uma troca de código de
    graça e, pior, a chance de amarrar a conta dele a outro membro."""
    if not disponivel():
        return False, "indisponivel"

    member_id, motivo = await consumir_state(state or "")
    if member_id is None:
        # Sem state válido não existe membro a quem atribuir o evento — este
        # é o único caminho que emite falha sem member_id (e é por desenho:
        # inventar um membro aqui seria pior que a linha sem dono).
        await emitir("google_connection_failed", motivo=motivo)
        return False, motivo

    if erro:
        # "access_denied" é a pessoa clicando em Cancelar: não é defeito.
        await emitir("google_connection_failed", member_id, motivo="negado_pelo_usuario")
        return False, "negado_pelo_usuario"
    if not code:
        await emitir("google_connection_failed", member_id, motivo="sem_code")
        return False, "sem_code"

    api = api or GoogleAPI()
    try:
        tokens = await api.trocar_code(code)
    except GoogleErro as falha:
        # `log.warning` com a exceção — a mensagem do Google fica no log do
        # servidor, nunca na tela nem no analytics.
        log.warning("Google: troca de code falhou (%s)", falha.motivo, exc_info=True)
        await emitir("google_connection_failed", member_id, motivo="troca_falhou")
        return False, "troca_falhou"

    # O Google deixa a pessoa DESMARCAR permissões na tela de consentimento: a
    # troca volta 200 com um `scope` menor do que o pedido (ou sem o campo).
    # Sem calendar.events nada do piloto funciona — guardar a conexão aqui
    # seria prometer um acesso que não temos e travar o /google depois.
    concedido = tokens.get("scope") or ""
    if ESCOPO not in concedido.split():
        log.warning("Google: consentimento sem calendar.events — nada foi guardado")
        await emitir("google_connection_failed", member_id, motivo="escopo_insuficiente")
        return False, "escopo_insuficiente"

    expira_em = None
    if segundos := tokens.get("expires_in"):
        expira_em = datetime.now(UTC) + timedelta(seconds=int(segundos))
    await salvar_conexao(
        member_id,
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        expira_em=expira_em,
        # O que fica guardado é o consentimento REAL, nunca o que pedimos.
        escopo=concedido,
    )
    # Payload sem token, sem code, sem e-mail: só o fato (ADR-005).
    await emitir(
        "google_connection_succeeded",
        member_id,
        com_refresh=bool(tokens.get("refresh_token")),
    )
    return True, "ok"


async def desconectar_membro(member_id: int) -> bool:
    """`desconectar` + analytics. É o que os comandos chamam."""
    havia = await desconectar(member_id)
    await emitir("google_disconnected", member_id, havia_conexao=havia)
    return havia


# ── evento de teste ──────────────────────────────────────────────────────

TITULO_TESTE = "Teste do Mordomo da Família"
MINUTOS_ATE_COMECAR = 5
MINUTOS_DE_DURACAO = 15

# Janela em que repetir /google_teste devolve o MESMO evento sem nem chamar o
# Google. Deslizante: conta do último evento criado.
IDEMPOTENCIA_MINUTOS = 10

# O Google exige que o id do evento seja base32hex: só `a`-`v` e `0`-`9`, de 5
# a 1024 caracteres. Hexadecimal cabe inteiro nesse alfabeto; o prefixo também.
_PREFIXO_ID = "mordomoteste"


def _id_do_evento(conexao: GoogleConnection) -> str:
    """Id determinístico do evento de teste: UM por conexão (ADR-010).

    É a segunda trava da idempotência — se o registro local não valer (banco
    restaurado, comando concorrente), o Google responde 409 em vez de criar um
    evento duplicado.

    NÃO deriva do relógio de propósito. Com balde de tempo, dois comandos
    simultâneos em lados opostos de uma virada geram ids diferentes e viram
    DOIS eventos: a fronteira é exatamente o caso "apertei duas vezes". A
    conexão não tem fronteira. O preço, aceito no piloto, é que o teste é
    sempre o mesmo evento enquanto a conexão for a mesma; /google_desconectar
    seguido de /google dá uma conexão nova — e um teste novo.

    `criado_em` entra junto do id porque chave primária PODE ser reaproveitada
    depois de um delete (o SQLite dos testes faz isso): sem ele, reconectar
    devolveria o id antigo e o teste novo morreria em 409."""
    nascimento = conexao.criado_em.isoformat() if conexao.criado_em else ""
    semente = f"{conexao.member_id}:{conexao.id}:{nascimento}"
    return f"{_PREFIXO_ID}{hashlib.sha256(semente.encode()).hexdigest()[:24]}"


def _corpo_do_evento(event_id: str, inicio: datetime) -> dict:
    fuso = ZoneInfo(settings.tz_familia)
    comeco = inicio.astimezone(fuso)
    fim = comeco + timedelta(minutes=MINUTOS_DE_DURACAO)
    return {
        "id": event_id,
        "summary": TITULO_TESTE,
        "description": (
            "Evento criado pelo Mordomo da Família para testar a conexão com o "
            "Google Calendar. Pode apagar."
        ),
        # timeZone junto do dateTime: sem ele o Google interpreta no fuso do
        # calendário, que não é necessariamente o da família.
        "start": {"dateTime": comeco.isoformat(), "timeZone": settings.tz_familia},
        "end": {"dateTime": fim.isoformat(), "timeZone": settings.tz_familia},
    }


async def _access_token_valido(
    conexao: GoogleConnection, api: GoogleAPI, agora: datetime
) -> tuple[str | None, str, bool]:
    """Devolve (access_token, motivo, renovou). Renova ANTES de vencer (margem
    de 60s) para não gastar uma chamada ao calendário só para tomar 401.

    `renovou` é o que separa "o token ainda valia" de "houve um refresh" no
    analytics — sem ele o KPI de custo do refresh não existe."""
    token = access_token_de(conexao)
    vencido = conexao.expira_em is not None and conexao.expira_em <= agora + timedelta(seconds=60)
    if token and not vencido:
        return token, "ok", False
    novo, motivo = await _renovar(conexao, api)
    return novo, motivo, novo is not None


async def _renovar(conexao: GoogleConnection, api: GoogleAPI) -> tuple[str | None, str]:
    """Troca o refresh_token por um access_token novo e o GUARDA cifrado.

    Sem refresh (ou com refresh recusado em definitivo) o caminho de volta é
    reconectar — não existe conserto automático para consentimento revogado.
    Falha transitória vira `rede_indisponivel` de propósito: só o motivo
    `reconectar` apaga credencial, e um blip não pode fazer isso."""
    refresh = refresh_token_de(conexao)
    if not refresh:
        # Sem refresh legível (nunca houve, ou a chave Fernet mudou) não há o
        # que renovar nem hoje nem nunca: é credencial morta, não espera.
        return None, "reconectar"
    try:
        tokens = await api.renovar(refresh)
    except GoogleErro as falha:
        log.warning("Google: refresh recusado (%s)", falha.motivo, exc_info=True)
        return None, "reconectar" if falha.permanente else "rede_indisponivel"
    expira_em = None
    if segundos := tokens.get("expires_in"):
        expira_em = datetime.now(UTC) + timedelta(seconds=int(segundos))
    await salvar_conexao(
        conexao.member_id,
        access_token=tokens["access_token"],
        # None de propósito: o refresh de rotina não devolve refresh_token e
        # salvar_conexao preserva o que já está lá.
        refresh_token=tokens.get("refresh_token"),
        expira_em=expira_em,
        # Refresh de rotina costuma repetir o scope; quando não vem, o que
        # vale é o que já estava guardado — nunca o que gostaríamos de ter.
        escopo=tokens.get("scope") or conexao.escopo or "",
    )
    return tokens["access_token"], "ok"


# Motivos que significam "essa credencial não volta mais": consentimento
# revogado, refresh recusado em definitivo, token ilegível por rotação de
# chave. Motivo transitório (rede, 5xx) NÃO entra aqui — apagar credencial por
# blip de rede faria a família reautorizar à toa.
MOTIVOS_DE_CREDENCIAL_MORTA = ("reconectar", "permissao_negada")


async def _esquecer_credencial(member_id: int, motivo: str) -> None:
    """Apaga a conexão inutilizável — é o que devolve o /google à pessoa.

    Sem isto existe deadlock: /google_teste responde "mande /google" e /google
    responde "já conectado", porque a linha morta continua no banco."""
    if await desconectar(member_id):
        log.warning("Google: credencial inutilizável esquecida (%s)", motivo)


async def _limpar_se_morta(member_id: int, motivo: str) -> None:
    if motivo in MOTIVOS_DE_CREDENCIAL_MORTA:
        await _esquecer_credencial(member_id, motivo)


async def _recusa(member_id: int, motivo: str) -> dict:
    """Emite a falha e, se o motivo for definitivo, limpa a credencial."""
    await emitir("google_test_event_failed", member_id, motivo=motivo)
    await _limpar_se_morta(member_id, motivo)
    return {"ok": False, "motivo": motivo, "link": None, "novo": False}


async def criar_evento_teste(
    member_id: int, api: GoogleAPI | None = None, agora: datetime | None = None
) -> dict:
    """Cria o evento de teste no calendário principal do membro.

    Devolve {"ok", "motivo", "link", "novo"}. Motivos de recusa, categóricos:
      indisponivel · desconectado · reconectar · permissao_negada ·
      rede_indisponivel · calendario_recusou

    `agora` é injetável para o teste fixar o relógio — nunca vem do usuário."""
    agora = agora or datetime.now(UTC)
    if not disponivel():
        return {"ok": False, "motivo": "indisponivel", "link": None, "novo": False}

    conexao = await conexao_de(member_id)
    if conexao is None:
        return {"ok": False, "motivo": "desconectado", "link": None, "novo": False}

    # Idempotência (1ª trava, local): repetiu dentro da janela → devolve o
    # mesmo evento sem chamar o Google.
    if conexao.teste_criado_em is not None and (
        agora - conexao.teste_criado_em < timedelta(minutes=IDEMPOTENCIA_MINUTOS)
    ):
        return {"ok": True, "motivo": "ja_criado", "link": conexao.teste_link, "novo": False}

    api = api or GoogleAPI()
    token, motivo, renovou = await _access_token_valido(conexao, api, agora)
    if token is None:
        return await _recusa(member_id, motivo)

    event_id = _id_do_evento(conexao)
    corpo = _corpo_do_evento(event_id, agora + timedelta(minutes=MINUTOS_ATE_COMECAR))

    try:
        criado = await api.criar_evento(token, corpo)
    except GoogleErro as falha:
        if falha.motivo == "token_expirado":
            # Token revogado com validade ainda "no papel": renova e tenta UMA
            # vez. Sem o limite, um 401 permanente viraria laço.
            token, motivo = await _renovar(conexao, api)
            if token is None:
                return await _recusa(member_id, motivo)
            renovou = True
            try:
                criado = await api.criar_evento(token, corpo)
            except GoogleErro as segunda:
                return await _falha_do_teste(member_id, segunda)
        elif falha.motivo == "evento_duplicado":
            # Idempotência (2ª trava, no Google): o evento já está lá. Isso é
            # sucesso — recriar seria a duplicata que queremos evitar. O link
            # vem do banco (e não do snapshot `conexao`, que pode estar velho:
            # quem venceu a corrida já gravou o link do MESMO evento).
            link = await _guardar_teste(member_id, event_id, None, agora)
            return {"ok": True, "motivo": "ja_criado", "link": link, "novo": False}
        else:
            return await _falha_do_teste(member_id, falha)

    link = criado.get("htmlLink")
    await _guardar_teste(member_id, event_id, link, agora)
    # Só o fato: nada de título, link ou token no analytics (ADR-005).
    # `renovou` distingue "o token ainda valia" de "precisei renovar antes".
    await emitir("google_test_event_created", member_id, renovou=renovou)
    return {"ok": True, "motivo": "ok", "link": link, "novo": True}


# ── agenda da conversa (o que o subagente Agenda usa) ────────────────────
#
# Mesma credencial, mesmas regras de refresh e os MESMOS motivos categóricos do
# /google_teste — o que muda é a origem: aqui o pedido nasce de uma conversa,
# dentro de um turno. Por isso nada daqui emite analytics: quem chama é uma
# tool, que emite `tool_result` com member/session/turn (regra nº 4). Emitir de
# dentro viraria linha órfã no funil.

# Prefixo do id do evento da conversa (base32hex: só `a`-`v` e `0`-`9`).
_PREFIXO_ID_CONVERSA = "mordomoevento"


def _id_do_evento_da_conversa(
    member_id: int, turn_id: str | None, inicio: datetime, fim: datetime, titulo: str
) -> str | None:
    """Id determinístico POR TURNO — a trava contra a duplicata do retry.

    O pipeline repete um turno inteiro quando o LLM trava (ADR-006); sem id
    fixo, a repetição criaria um segundo evento no calendário da pessoa. Com
    ele o Google devolve 409 e quem chama lê isso como "já está lá".

    Pedir de novo mais tarde é outro `turn_id` — e aí PODE virar outro evento,
    que é o certo: repetir um pedido é um pedido novo.

    O título entra só como SEMENTE do sha256: o que viaja é o digest, que não
    volta em analytics nem em log. Ele está aí para dois eventos diferentes
    marcados no mesmo turno ("almoço 12h e jantar 20h") não colidirem.

    Sem `turn_id` (chamada fora de turno) devolvemos None e deixamos o Google
    gerar o id: um id fixo demais duplicaria pedidos legítimos."""
    if not turn_id:
        return None
    semente = f"{member_id}:{turn_id}:{inicio.isoformat()}:{fim.isoformat()}:{titulo}"
    return f"{_PREFIXO_ID_CONVERSA}{hashlib.sha256(semente.encode()).hexdigest()[:24]}"


def _corpo_do_evento_da_conversa(
    event_id: str | None, titulo: str, inicio: datetime, fim: datetime, local: str | None
) -> dict:
    fuso = ZoneInfo(settings.tz_familia)
    corpo: dict = {
        "summary": titulo,
        # timeZone junto do dateTime: sem ele o Google interpreta no fuso do
        # calendário, que não é necessariamente o da família.
        "start": {
            "dateTime": inicio.astimezone(fuso).isoformat(),
            "timeZone": settings.tz_familia,
        },
        "end": {
            "dateTime": fim.astimezone(fuso).isoformat(),
            "timeZone": settings.tz_familia,
        },
    }
    if event_id:
        corpo["id"] = event_id
    if local:
        corpo["location"] = local
    return corpo


async def _com_token(conexao: GoogleConnection, api: GoogleAPI, agora: datetime, operacao):
    """Roda `operacao(access_token)` com credencial válida.

    Devolve (resultado, motivo, renovou). `resultado is None` significa que não
    há credencial utilizável e `motivo` diz o caminho de volta. GoogleErro da
    operação SOBE: o que 409/403/5xx significam é decisão de quem chamou.

    Existe para o criar e o listar da conversa compartilharem exatamente as
    regras de refresh já testadas (`_access_token_valido` + `_renovar`)."""
    token, motivo, renovou = await _access_token_valido(conexao, api, agora)
    if token is None:
        return None, motivo, renovou
    try:
        resultado = await operacao(token)
    except GoogleErro as falha:
        if falha.motivo != "token_expirado":
            raise
    else:
        return resultado, "ok", renovou
    # Token revogado com validade ainda "no papel": renova e tenta UMA vez.
    # Sem o limite, um 401 permanente viraria laço.
    token, motivo = await _renovar(conexao, api)
    if token is None:
        return None, motivo, renovou
    return await operacao(token), "ok", True


async def _recusa_da_conversa(member_id: int, motivo: str) -> dict:
    await _limpar_se_morta(member_id, motivo)
    return {"ok": False, "motivo": motivo}


async def criar_evento_na_agenda(
    member_id: int,
    *,
    titulo: str,
    inicio: datetime,
    fim: datetime,
    local: str | None = None,
    turn_id: str | None = None,
    api: GoogleAPI | None = None,
    agora: datetime | None = None,
) -> dict:
    """Cria no calendário principal do membro o evento pedido na conversa.

    Devolve {"ok", "motivo", "link", "novo", "renovou"}. Motivos de recusa,
    categóricos: indisponivel · desconectado · reconectar · permissao_negada ·
    rede_indisponivel · calendario_recusou.

    NUNCA devolve ok=True sem o Google ter aceitado: quem chama não pode ter
    como "fingir sucesso" — foi assim que um almoço real virou linha no banco e
    nada no calendário."""
    agora = agora or datetime.now(UTC)
    if not disponivel():
        return {"ok": False, "motivo": "indisponivel"}
    conexao = await conexao_de(member_id)
    if conexao is None:
        return {"ok": False, "motivo": "desconectado"}

    api_propria = api is None
    api = api or GoogleAPI()
    try:
        event_id = _id_do_evento_da_conversa(member_id, turn_id, inicio, fim, titulo)
        corpo = _corpo_do_evento_da_conversa(event_id, titulo, inicio, fim, local)
        try:
            criado, motivo, renovou = await _com_token(
                conexao, api, agora, lambda token: api.criar_evento(token, corpo)
            )
        except GoogleErro as falha:
            if falha.motivo == "evento_duplicado":
                # Retry do MESMO turno: o evento já está lá. Sucesso — recriar é a
                # duplicata que o id determinístico existe para impedir.
                return {"ok": True, "motivo": "ja_criado", "link": None, "novo": False}
            log.warning("Google: evento da conversa falhou (%s)", falha.motivo, exc_info=falha)
            return await _recusa_da_conversa(member_id, falha.motivo)
        if criado is None:
            return await _recusa_da_conversa(member_id, motivo)
        return {
            "ok": True,
            "motivo": "ok",
            "link": criado.get("htmlLink"),
            "novo": True,
            "renovou": renovou,
        }
    finally:
        if api_propria:
            await api.fechar()


def _instante_do_google(campo: dict) -> tuple[datetime | None, bool]:
    """(quando, dia_inteiro) a partir de um `start`/`end` do Calendar.

    Evento com hora vem em `dateTime`; evento de dia inteiro vem em `date`
    (só a data, sem fuso) — ler só `dateTime` faria o feriado sumir da lista."""
    if bruto := campo.get("dateTime"):
        try:
            return datetime.fromisoformat(bruto), False
        except ValueError:
            return None, False
    if bruto := campo.get("date"):
        try:
            dia = datetime.fromisoformat(bruto)
        except ValueError:
            return None, True
        return dia.replace(tzinfo=ZoneInfo(settings.tz_familia)), True
    return None, False


def _normalizar_evento(item: dict) -> dict | None:
    inicio, dia_inteiro = _instante_do_google(item.get("start") or {})
    if inicio is None:
        return None
    fim, _ = _instante_do_google(item.get("end") or {})
    return {
        "titulo": item.get("summary") or "(sem título)",
        "inicio": inicio,
        "fim": fim,
        "dia_inteiro": dia_inteiro,
        "local": item.get("location"),
    }


async def listar_eventos_da_agenda(
    member_id: int,
    *,
    inicio: datetime,
    fim: datetime,
    maximo: int = 20,
    api: GoogleAPI | None = None,
    agora: datetime | None = None,
) -> dict:
    """Lê a janela [inicio, fim) do calendário principal do membro.

    Devolve {"ok", "motivo", "eventos"} — cada evento já normalizado
    ({titulo, inicio, fim, dia_inteiro, local}). Mesmos motivos categóricos da
    criação."""
    agora = agora or datetime.now(UTC)
    if not disponivel():
        return {"ok": False, "motivo": "indisponivel", "eventos": []}
    conexao = await conexao_de(member_id)
    if conexao is None:
        return {"ok": False, "motivo": "desconectado", "eventos": []}

    api_propria = api is None
    api = api or GoogleAPI()
    try:
        try:
            itens, motivo, _ = await _com_token(
                conexao,
                api,
                agora,
                lambda token: api.listar_eventos(token, inicio=inicio, fim=fim, maximo=maximo),
            )
        except GoogleErro as falha:
            log.warning("Google: leitura da agenda falhou (%s)", falha.motivo, exc_info=falha)
            recusa = await _recusa_da_conversa(member_id, falha.motivo)
            return {**recusa, "eventos": []}
        if itens is None:
            recusa = await _recusa_da_conversa(member_id, motivo)
            return {**recusa, "eventos": []}
        eventos = [e for item in itens if (e := _normalizar_evento(item))]
        return {"ok": True, "motivo": "ok", "eventos": eventos}
    finally:
        if api_propria:
            await api.fechar()


# ── Respostas dos comandos (texto puro, canal-agnóstico — ADR-001) ───────
#
# Tom de mordomo, curto, sem markdown pesado (regra nº 6): o destino é o
# WhatsApp, onde negrito e tabela viram sujeira. Nenhum texto daqui expõe
# motivo técnico, código HTTP ou nome de variável — o detalhe fica no log.

INDISPONIVEL = (
    "Ainda não estou habilitado a cuidar do Google Agenda por aqui. 🙏 "
    "Assim que isso for configurado, eu aviso."
)

PAINEL_GOOGLE = "myaccount.google.com/permissions"


def _texto_conectar(url: str) -> str:
    return (
        "Para eu cuidar da sua agenda, preciso da sua autorização no Google. 🤵\n\n"
        f"{url}\n\n"
        "Abra o link e entre na SUA conta. O que eu peço é só isto: ver e criar "
        "eventos no seu Google Agenda. Não peço e-mail, contatos nem arquivos.\n"
        "Sua senha fica com o Google — eu nunca vejo, nunca guardo.\n\n"
        f"O link vale {VALIDADE_STATE_MINUTOS} minutos e é só seu: não repasse."
    )


TEXTO_JA_CONECTADO = (
    "Seu Google Agenda já está conectado. ✅\n\n"
    "Quer conferir? /google_teste cria um evento de teste na sua agenda.\n"
    "Prefere encerrar? /google_desconectar"
)

TEXTO_PRECISA_CONECTAR = (
    "Seu Google Agenda ainda não está conectado por aqui. "
    "Mande /google que eu te passo o link. 🤵"
)

TEXTO_RECONECTAR = (
    "Perdi a autorização da sua agenda — pode ter sido revogada no Google. "
    "Mande /google para me autorizar de novo. 🙏"
)

# Motivo categórico → o que a família lê. Motivo que não estiver aqui cai no
# texto genérico: é melhor uma desculpa educada que um vazamento de detalhe.
_TEXTO_DE_FALHA = {
    "indisponivel": INDISPONIVEL,
    "desconectado": TEXTO_PRECISA_CONECTAR,
    "reconectar": TEXTO_RECONECTAR,
    "permissao_negada": TEXTO_RECONECTAR,
    "rede_indisponivel": (
        "O Google não me respondeu agora. 😕 Tente de novo daqui a pouquinho."
    ),
}
TEXTO_FALHA_GENERICA = (
    "Não consegui criar o evento de teste agora. 😕 Tente de novo daqui a pouco — "
    "se insistir, me chame com /google para reconectar."
)


def _texto_do_teste(resultado: dict) -> str:
    if not resultado["ok"]:
        return _TEXTO_DE_FALHA.get(resultado["motivo"], TEXTO_FALHA_GENERICA)
    link = resultado.get("link")
    ver = f"\n\nVer no Google: {link}" if link else ""
    if resultado["novo"]:
        return (
            f'Pronto! Coloquei "{TITULO_TESTE}" na sua agenda, '
            f"começando em {MINUTOS_ATE_COMECAR} minutos e durando "
            f"{MINUTOS_DE_DURACAO}.{ver}\n\n"
            "Se aparecer aí, está tudo certo — pode apagar depois. 🤵"
        )
    return (
        "Esse evento de teste eu já tinha criado há pouco — é o mesmo, "
        f"não vou duplicar.{ver}"
    )


async def responder_comando(
    comando: str, member_id: int, api: GoogleAPI | None = None
) -> str | None:
    """Texto de resposta de /google, /google_teste e /google_desconectar.

    Devolve None para comando que não é nosso (o adapter segue o fluxo normal).
    Quem checa "é privado?" e resolve o membro é `channels/comandos.py` — aqui
    já se assume borda validada (ADR-003: member_id nunca vem do texto).

    `api` existe para o teste injetar transporte falso, como o `cliente` do
    WhatsAppAPI — em produção fica None e o cliente real é criado na hora."""
    if comando == "google":
        if not disponivel():
            return INDISPONIVEL
        conexao = await conexao_de(member_id)
        if conexao is not None:
            if credencial_utilizavel(conexao):
                return TEXTO_JA_CONECTADO
            # Linha existe mas nada nela abre (GOOGLE_TOKEN_KEY rotacionada,
            # dado alterado): esquecer AQUI é o que deixa a pessoa autorizar de
            # novo em vez de ouvir "já está conectado" para sempre.
            await _esquecer_credencial(member_id, "credencial_ilegivel")
        url = await iniciar_conexao(member_id)
        return _texto_conectar(url) if url else INDISPONIVEL

    if comando == "google_teste":
        if not disponivel():
            return INDISPONIVEL
        return _texto_do_teste(await criar_evento_teste(member_id, api=api))

    if comando == "google_desconectar":
        # Funciona MESMO com a integração desligada: quem já conectou tem que
        # conseguir sair, ainda que alguém tenha tirado a configuração depois.
        havia = await desconectar_membro(member_id)
        if havia:
            return (
                "Pronto: esqueci sua conta do Google. 🤵 Não guardo mais nenhum "
                "acesso à sua agenda.\n\n"
                "Para cortar também do lado do Google, revogue o acesso em "
                f"{PAINEL_GOOGLE}."
            )
        return (
            "Não havia nenhuma conta do Google conectada por aqui — está tudo limpo. 🤵\n\n"
            f"Se quiser conferir do lado de lá: {PAINEL_GOOGLE}"
        )

    return None


async def _falha_do_teste(member_id: int, falha: GoogleErro) -> dict:
    # exc_info=falha (e não True): esta função é CHAMADA de dentro do except,
    # então `sys.exc_info()` já não é confiável aqui — a exceção vem explícita.
    log.warning("Google: evento de teste falhou (%s)", falha.motivo, exc_info=falha)
    return await _recusa(member_id, falha.motivo)
