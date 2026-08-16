"""/google_teste: cria UM evento no calendário principal, sem duplicar.

Todo o Google é falso aqui (httpx.MockTransport). O que se testa é o que o
piloto promete: evento no `primary`, começando em 5 minutos, 15 de duração, no
fuso da família, e uma repetição imediata do comando NÃO gera um segundo."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select, update

from apoio import criar_membro as _membro
from apoio import google_configurado
from mordomo.db.models import GoogleConnection, ProductEvent
from mordomo.db.session import Sessao
from mordomo.integracoes import google
from mordomo.integracoes.google_api import GoogleAPI, GoogleErro

LINK = "https://calendar.google.com/event?eid=abc"


def _api_de(handler) -> GoogleAPI:
    return GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _gravador(respostas=None):
    """Handler que guarda cada requisição e responde 200 com um evento criado."""
    chamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        if respostas:
            return respostas.pop(0)(request)
        return httpx.Response(200, json={"id": "evt-1", "htmlLink": LINK})

    handler.chamadas = chamadas
    return handler


async def _conectado(nome: str, *, expira_em_min: int = 60):
    membro = await _membro(nome)
    await google.salvar_conexao(
        membro.id,
        access_token="access-valido",
        refresh_token="refresh-valido",
        expira_em=datetime.now(UTC) + timedelta(minutes=expira_em_min),
    )
    return membro


# ── o evento ─────────────────────────────────────────────────────────────


async def test_cria_evento_no_primary_com_horario_e_fuso_certos():
    with google_configurado():
        membro = await _conectado("TesteEvento")
        handler = _gravador()
        agora = datetime(2026, 8, 16, 14, 0, tzinfo=UTC)
        resultado = await google.criar_evento_teste(
            membro.id, api=_api_de(handler), agora=agora
        )

    assert resultado["ok"] is True and resultado["novo"] is True
    assert resultado["link"] == LINK

    pedido = handler.chamadas[0]
    assert "/calendars/primary/events" in str(pedido.url)
    assert pedido.headers["Authorization"] == "Bearer access-valido"

    corpo = json.loads(pedido.content)
    assert corpo["summary"] == "Teste do Mordomo da Família"

    fuso = ZoneInfo("America/Sao_Paulo")
    inicio = datetime.fromisoformat(corpo["start"]["dateTime"])
    fim = datetime.fromisoformat(corpo["end"]["dateTime"])
    assert inicio == (agora + timedelta(minutes=5)).astimezone(fuso)
    assert fim - inicio == timedelta(minutes=15)
    assert corpo["start"]["timeZone"] == "America/Sao_Paulo"
    assert corpo["end"]["timeZone"] == "America/Sao_Paulo"


async def test_evento_leva_id_deterministico_aceito_pelo_google():
    """O Google exige base32hex no id (só a-v e 0-9) e recusa o resto com 400."""
    with google_configurado():
        membro = await _conectado("TesteIdValido")
        handler = _gravador()
        await google.criar_evento_teste(membro.id, api=_api_de(handler))

    identificador = json.loads(handler.chamadas[0].content)["id"]
    assert 5 <= len(identificador) <= 1024
    assert set(identificador) <= set("abcdefghijklmnopqrstuv0123456789")


# ── idempotência ─────────────────────────────────────────────────────────


async def test_repetir_o_comando_na_hora_nao_cria_segundo_evento():
    with google_configurado():
        membro = await _conectado("TesteIdempotente")
        handler = _gravador()
        api = _api_de(handler)
        agora = datetime(2026, 8, 16, 14, 0, tzinfo=UTC)

        primeiro = await google.criar_evento_teste(membro.id, api=api, agora=agora)
        segundo = await google.criar_evento_teste(
            membro.id, api=api, agora=agora + timedelta(seconds=30)
        )

    assert primeiro["novo"] is True
    assert segundo["ok"] is True and segundo["novo"] is False
    assert segundo["link"] == LINK
    assert len(handler.chamadas) == 1, "o segundo comando não pode chamar o Google"


async def test_depois_da_janela_quem_decide_e_o_google():
    """Idempotência local é uma JANELA: passada ela, perguntamos ao Google de
    novo — e é o 409 dele (mesmo id) que impede a duplicata."""
    with google_configurado():
        membro = await _conectado("TesteJanela")
        handler = _gravador([
            lambda r: httpx.Response(200, json={"id": "evt-1", "htmlLink": LINK}),
            lambda r: httpx.Response(409, json={"error": "duplicate"}),
        ])
        api = _api_de(handler)
        agora = datetime(2026, 8, 16, 14, 0, tzinfo=UTC)

        await google.criar_evento_teste(membro.id, api=api, agora=agora)
        depois = await google.criar_evento_teste(
            membro.id,
            api=api,
            agora=agora + timedelta(minutes=google.IDEMPOTENCIA_MINUTOS + 1),
        )

    assert depois["ok"] is True and depois["novo"] is False
    assert len(handler.chamadas) == 2


async def _esquecer_registro_local(member_id: int) -> None:
    """Simula o comando CONCORRENTE: o segundo /google_teste leu a conexão
    ANTES de o primeiro gravar teste_criado_em, então a trava local não vale
    para ele — só o id determinístico segura a duplicata."""
    async with Sessao() as s:
        await s.execute(
            update(GoogleConnection)
            .where(GoogleConnection.member_id == member_id)
            .values(teste_event_id=None, teste_link=None, teste_criado_em=None)
        )
        await s.commit()


async def test_id_do_evento_nao_muda_na_virada_de_um_balde_de_tempo():
    """Dois comandos simultâneos em lados opostos de uma fronteira de 10 min
    precisam mandar o MESMO id — senão o Google cria dois eventos, que é
    justamente o que a idempotência existe para impedir."""
    with google_configurado():
        membro = await _conectado("TesteFronteira")
        handler = _gravador([
            lambda r: httpx.Response(200, json={"id": "evt-1", "htmlLink": LINK}),
            # o Google recusa o id repetido — a 2ª trava fazendo o trabalho
            lambda r: httpx.Response(409, json={"error": "duplicate"}),
        ])
        api = _api_de(handler)
        antes = datetime(2026, 8, 16, 14, 59, 59, tzinfo=UTC)
        depois = datetime(2026, 8, 16, 15, 0, 1, tzinfo=UTC)
        assert int(antes.timestamp()) // 600 != int(depois.timestamp()) // 600, "é fronteira"

        primeiro = await google.criar_evento_teste(membro.id, api=api, agora=antes)
        await _esquecer_registro_local(membro.id)
        segundo = await google.criar_evento_teste(membro.id, api=api, agora=depois)

    ids = [json.loads(c.content)["id"] for c in handler.chamadas]
    assert ids[0] == ids[1], "o id do evento não pode depender do relógio"
    assert primeiro["novo"] is True
    assert segundo["ok"] is True and segundo["novo"] is False, "uma única criação"


async def test_reconectar_libera_um_evento_de_teste_novo():
    """O id é estável POR CONEXÃO (ADR-010): enquanto a conexão for a mesma, o
    teste é sempre o mesmo evento. Quem quiser um novo, desconecta e conecta —
    e aí o id muda."""
    with google_configurado():
        membro = await _conectado("TesteIdPorConexao")
        handler = _gravador()
        api = _api_de(handler)
        await google.criar_evento_teste(membro.id, api=api)

        await google.desconectar(membro.id)
        await google.salvar_conexao(
            membro.id,
            access_token="access-valido",
            refresh_token="refresh-valido",
            expira_em=datetime.now(UTC) + timedelta(minutes=60),
        )
        novo = await google.criar_evento_teste(membro.id, api=api)

    ids = [json.loads(c.content)["id"] for c in handler.chamadas]
    assert novo["novo"] is True
    assert ids[0] != ids[1]


async def test_evento_ja_existente_no_google_conta_como_sucesso():
    """409: nosso registro local se perdeu mas o evento existe lá. Isso é
    sucesso — recriar seria justamente a duplicata que queremos evitar."""
    with google_configurado():
        membro = await _conectado("TesteConflito")
        handler = _gravador([lambda r: httpx.Response(409, json={"error": "duplicate"})])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

    assert resultado["ok"] is True and resultado["novo"] is False


class _ApiQuePerdeACorrida:
    """Simula a corrida de dois /google_teste, de forma determinística.

    B leu a conexão ANTES de A gravar (snapshot com teste_link vazio). Quando B
    chama o Google, A já terminou: o evento existe e o link está no banco. B
    recebe 409 — e não pode escrever o None do snapshot velho por cima."""

    def __init__(self, member_id: int, quando: datetime) -> None:
        self._member_id = member_id
        self._quando = quando

    async def criar_evento(self, access_token: str, evento: dict) -> dict:
        await google._guardar_teste(self._member_id, evento["id"], LINK, self._quando)
        raise GoogleErro("evento_duplicado", 409)


async def test_409_concorrente_nao_apaga_o_link_que_o_outro_gravou():
    with google_configurado():
        membro = await _conectado("TesteCorrida409")
        agora = datetime(2026, 8, 16, 14, 0, tzinfo=UTC)
        api = _ApiQuePerdeACorrida(membro.id, agora)

        resultado = await google.criar_evento_teste(membro.id, api=api, agora=agora)
        conexao = await google.conexao_de(membro.id)

    assert resultado["ok"] is True and resultado["novo"] is False
    assert conexao.teste_link == LINK, "o 409 não pode zerar o link já gravado"
    assert resultado["link"] == LINK, "e a resposta tem que mostrar o link que existe"


# ── token expirado ───────────────────────────────────────────────────────


async def test_token_vencido_e_renovado_antes_de_criar():
    with google_configurado():
        membro = await _conectado("TesteRenova", expira_em_min=-5)
        handler = _gravador([
            lambda r: httpx.Response(200, json={"access_token": "access-novo", "expires_in": 3599}),
            lambda r: httpx.Response(200, json={"id": "evt-2", "htmlLink": LINK}),
        ])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

        assert resultado["ok"] is True
        assert "oauth2.googleapis.com/token" in str(handler.chamadas[0].url)
        assert handler.chamadas[1].headers["Authorization"] == "Bearer access-novo"

        # o token renovado tem que FICAR guardado (cifrado), senão renovamos
        # a cada comando — e o refresh antigo tem que sobreviver
        conexao = await google.conexao_de(membro.id)
        assert google.access_token_de(conexao) == "access-novo"
        assert google.refresh_token_de(conexao) == "refresh-valido"


async def test_401_do_calendario_renova_e_tenta_uma_vez():
    """O relógio pode estar certo e o token revogado assim mesmo."""
    with google_configurado():
        membro = await _conectado("Teste401")
        handler = _gravador([
            lambda r: httpx.Response(401, json={"error": "invalid_credentials"}),
            lambda r: httpx.Response(200, json={"access_token": "access-novo", "expires_in": 3599}),
            lambda r: httpx.Response(200, json={"id": "evt-3", "htmlLink": LINK}),
        ])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

    assert resultado["ok"] is True
    assert len(handler.chamadas) == 3


async def test_refresh_invalido_pede_reconexao():
    """Consentimento revogado no painel do Google: o caminho de volta é
    /google, não uma desculpa genérica."""
    with google_configurado():
        membro = await _conectado("TesteRefreshRuim", expira_em_min=-5)
        handler = _gravador([lambda r: httpx.Response(400, json={"error": "invalid_grant"})])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

    assert resultado["ok"] is False and resultado["motivo"] == "reconectar"


async def test_refresh_invalido_esquece_a_credencial_morta():
    """Refresh recusado com 4xx é definitivo: o consentimento não existe mais.
    Deixar a linha no banco prende a pessoa — /google_teste manda usar /google
    e /google responde 'já conectado'."""
    with google_configurado():
        membro = await _conectado("TesteRefreshMorto", expira_em_min=-5)
        handler = _gravador([lambda r: httpx.Response(400, json={"error": "invalid_grant"})])
        await google.criar_evento_teste(membro.id, api=_api_de(handler))

        assert await google.conexao_de(membro.id) is None


async def test_permissao_negada_pelo_calendario_tambem_esquece_a_credencial():
    """403 no insert: o grant não dá mais o que precisamos. Mesmo desfecho —
    a pessoa tem que conseguir autorizar de novo."""
    with google_configurado():
        membro = await _conectado("TestePermissaoNegada")
        handler = _gravador([lambda r: httpx.Response(403, json={"error": "forbidden"})])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

        assert resultado["motivo"] == "permissao_negada"
        assert await google.conexao_de(membro.id) is None


def _corpo_de_quota(razao: str) -> dict:
    """Formato real do erro de limite do Google: a razão vive em
    `error.errors[].reason` — o `code` sozinho (403) não distingue quota de
    permissão de verdade."""
    return {
        "error": {
            "errors": [{"domain": "usageLimits", "reason": razao, "message": "limite"}],
            "code": 403,
            "message": "Rate Limit Exceeded",
        }
    }


@pytest.mark.parametrize(
    "razao",
    ["rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"],
)
async def test_403_de_quota_no_calendario_nao_apaga_a_credencial(razao):
    """403 do Google é ambíguo: permissão insuficiente (definitivo) OU limite
    de uso estourado (transitório). Tratar quota como permissão negada faria a
    família reautorizar por um pico de tráfego."""
    with google_configurado():
        membro = await _conectado(f"TesteQuota{razao}")
        corpo = _corpo_de_quota(razao)
        handler = _gravador([lambda r: httpx.Response(403, json=corpo)])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

        assert resultado["ok"] is False
        assert resultado["motivo"] == "rede_indisponivel", "quota é transitória"
        assert await google.conexao_de(membro.id) is not None, "quota não apaga nada"


async def test_403_de_permissao_de_verdade_continua_apagando_a_credencial():
    """A contrapartida: 403 sem razão de limite é consentimento que não serve
    mais — esse tem que continuar limpando a linha morta."""
    with google_configurado():
        membro = await _conectado("TestePermissaoReal")
        corpo = {
            "error": {
                "errors": [{"domain": "global", "reason": "forbidden"}],
                "code": 403,
                "message": "Insufficient Permission",
            }
        }
        handler = _gravador([lambda r: httpx.Response(403, json=corpo)])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

        assert resultado["motivo"] == "permissao_negada"
        assert await google.conexao_de(membro.id) is None


async def test_429_no_refresh_nao_apaga_a_conexao():
    """429 é 4xx, mas é 'volte depois', não 'não te conheço mais'."""
    with google_configurado():
        membro = await _conectado("TesteRefresh429", expira_em_min=-5)
        handler = _gravador([
            lambda r: httpx.Response(429, json={"error": "rate_limit_exceeded"})
        ])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

        assert resultado["motivo"] == "rede_indisponivel"
        assert await google.conexao_de(membro.id) is not None


async def test_408_no_refresh_nao_apaga_a_conexao():
    """Timeout de requisição idem: transitório, ainda que 4xx."""
    with google_configurado():
        membro = await _conectado("TesteRefresh408", expira_em_min=-5)
        handler = _gravador([lambda r: httpx.Response(408, text="request timeout")])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

        assert resultado["motivo"] == "rede_indisponivel"
        assert await google.conexao_de(membro.id) is not None


async def test_google_fora_do_ar_no_refresh_nao_apaga_a_conexao():
    """Rede caída não é consentimento revogado. Apagar a credencial por um
    blip obrigaria a família a reautorizar sem necessidade nenhuma."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    with google_configurado():
        membro = await _conectado("TesteRefreshSemRede", expira_em_min=-5)
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

        assert resultado["ok"] is False and resultado["motivo"] == "rede_indisponivel"
        assert await google.conexao_de(membro.id) is not None, "blip de rede não apaga nada"


async def test_google_com_erro_de_servidor_no_refresh_nao_apaga_a_conexao():
    """5xx é problema do lado de lá, não consentimento revogado."""
    with google_configurado():
        membro = await _conectado("TesteRefresh500", expira_em_min=-5)
        handler = _gravador([lambda r: httpx.Response(503, text="manutenção")])
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

        assert resultado["motivo"] == "rede_indisponivel"
        assert await google.conexao_de(membro.id) is not None


async def test_sem_refresh_token_e_access_vencido_pede_reconexao():
    with google_configurado():
        membro = await _membro("TesteSemRefresh")
        await google.salvar_conexao(
            membro.id,
            access_token="access-velho",
            refresh_token=None,
            expira_em=datetime.now(UTC) - timedelta(minutes=5),
        )
        handler = _gravador()
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

    assert resultado["ok"] is False and resultado["motivo"] == "reconectar"
    assert handler.chamadas == []


# ── caminhos de recusa ───────────────────────────────────────────────────


async def test_sem_conexao_nao_chama_o_google():
    with google_configurado():
        membro = await _membro("TesteDesconectado")
        handler = _gravador()
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

    assert resultado["ok"] is False and resultado["motivo"] == "desconectado"
    assert handler.chamadas == []


async def test_integracao_desligada_recusa_com_motivo_proprio():
    membro = await _membro("TesteDesligado")
    with google_configurado(client_id=""):
        resultado = await google.criar_evento_teste(membro.id)
    assert resultado["ok"] is False and resultado["motivo"] == "indisponivel"


async def test_google_fora_do_ar_nao_vira_traceback():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    with google_configurado():
        membro = await _conectado("TesteSemRede")
        resultado = await google.criar_evento_teste(membro.id, api=_api_de(handler))

    assert resultado["ok"] is False and resultado["motivo"] == "rede_indisponivel"


# ── analytics ────────────────────────────────────────────────────────────


async def test_analytics_do_teste_nao_leva_titulo_nem_token():
    with google_configurado():
        membro = await _conectado("TesteAnalytics")
        await google.criar_evento_teste(membro.id, api=_api_de(_gravador()))

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(
                ProductEvent.member_id == membro.id,
                ProductEvent.tipo == "google_test_event_created",
            )
        )
        eventos = list(res.scalars().all())

    assert eventos, "criar evento de teste tem que virar fato de produto"
    bruto = repr([e.payload for e in eventos])
    for proibido in ("Teste do Mordomo", "access-valido", "refresh-valido", LINK):
        assert proibido not in bruto


async def _ultimo_evento(member_id: int, tipo: str) -> ProductEvent:
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent)
            .where(ProductEvent.member_id == member_id, ProductEvent.tipo == tipo)
            .order_by(ProductEvent.id.desc())
        )
        return res.scalars().first()


async def test_analytics_diz_que_nao_houve_renovacao_com_token_ainda_valido():
    """`renovou` é o KPI que responde 'quanto do custo é refresh?'. Com token
    válido a resposta é False — e tem que ser verdade, não constante."""
    with google_configurado():
        membro = await _conectado("TesteRenovouNao")
        await google.criar_evento_teste(membro.id, api=_api_de(_gravador()))

    evento = await _ultimo_evento(membro.id, "google_test_event_created")
    assert evento is not None and evento.payload["renovou"] is False


async def test_analytics_diz_que_houve_renovacao_quando_o_token_venceu():
    with google_configurado():
        membro = await _conectado("TesteRenovouSim", expira_em_min=-5)
        handler = _gravador([
            lambda r: httpx.Response(200, json={"access_token": "access-novo", "expires_in": 3599}),
            lambda r: httpx.Response(200, json={"id": "evt-9", "htmlLink": LINK}),
        ])
        await google.criar_evento_teste(membro.id, api=_api_de(handler))

    evento = await _ultimo_evento(membro.id, "google_test_event_created")
    assert evento is not None and evento.payload["renovou"] is True
    # e continua sem nada sensível junto do fato
    for proibido in ("access-novo", "refresh-valido", LINK):
        assert proibido not in repr(evento.payload)


async def test_analytics_marca_renovacao_tambem_no_caminho_do_401():
    """Token 'no papel' válido, revogado na prática: houve refresh, e o
    analytics tem que dizer isso."""
    with google_configurado():
        membro = await _conectado("TesteRenovou401")
        handler = _gravador([
            lambda r: httpx.Response(401, json={"error": "invalid_credentials"}),
            lambda r: httpx.Response(200, json={"access_token": "access-novo", "expires_in": 3599}),
            lambda r: httpx.Response(200, json={"id": "evt-10", "htmlLink": LINK}),
        ])
        await google.criar_evento_teste(membro.id, api=_api_de(handler))

    evento = await _ultimo_evento(membro.id, "google_test_event_created")
    assert evento is not None and evento.payload["renovou"] is True


async def test_analytics_de_falha_traz_motivo_categorico():
    with google_configurado():
        membro = await _conectado("TesteAnalyticsFalha", expira_em_min=-5)
        handler = _gravador([lambda r: httpx.Response(400, json={"error": "invalid_grant"})])
        await google.criar_evento_teste(membro.id, api=_api_de(handler))

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(
                ProductEvent.member_id == membro.id,
                ProductEvent.tipo == "google_test_event_failed",
            )
        )
        eventos = list(res.scalars().all())

    assert eventos and eventos[-1].payload["motivo"] == "reconectar"


async def test_desconectar_emite_evento():
    with google_configurado():
        membro = await _conectado("TesteDesconectaEvento")
        await google.desconectar_membro(membro.id)

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(
                ProductEvent.member_id == membro.id,
                ProductEvent.tipo == "google_disconnected",
            )
        )
        assert res.scalars().all()
