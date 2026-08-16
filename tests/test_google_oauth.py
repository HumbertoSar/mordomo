"""OAuth do piloto Google: state (CSRF/replay/vinculação), URL de consentimento
e o callback ponta a ponta — com transporte HTTP falso, sem rede (regra nº 7)."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from apoio import criar_membro as _membro
from apoio import google_configurado
from mordomo.db.models import OAuthState, ProductEvent
from mordomo.db.session import Sessao
from mordomo.integracoes import google
from mordomo.integracoes.google_api import GoogleAPI

# ── state ────────────────────────────────────────────────────────────────


async def test_state_valido_devolve_o_membro():
    membro = await _membro("StateOk")
    with google_configurado():
        state = await google.criar_state(membro.id)
        assert await google.consumir_state(state) == (membro.id, "ok")


async def test_state_e_opaco_e_aleatorio():
    """Se o state fosse derivado do membro, forjar conexão alheia seria
    adivinhar um número. Dois states do MESMO membro têm que diferir — é o que
    prova que ele é aleatório, e não uma função do id."""
    membro = await _membro("StateOpaco")
    with google_configurado():
        primeiro = await google.criar_state(membro.id)
        segundo = await google.criar_state(membro.id)
    assert primeiro != segundo
    assert len(primeiro) >= 32
    # ...e o membro do segundo state continua sendo o certo (opaco ≠ anônimo)
    with google_configurado():
        assert (await google.consumir_state(segundo))[0] == membro.id


async def test_state_nao_fica_em_claro_no_banco():
    """Dump do banco não pode permitir forjar um callback."""
    membro = await _membro("StateHash")
    with google_configurado():
        state = await google.criar_state(membro.id)
    async with Sessao() as s:
        res = await s.execute(select(OAuthState).where(OAuthState.member_id == membro.id))
        registro = res.scalar_one()
    assert registro.state_hash != state
    assert state not in registro.state_hash


async def test_state_e_de_uso_unico():
    """Replay do callback (F5, histórico do navegador) não reabre a troca."""
    membro = await _membro("StateReuso")
    with google_configurado():
        state = await google.criar_state(membro.id)
        assert (await google.consumir_state(state))[1] == "ok"
        assert await google.consumir_state(state) == (None, "state_usado")


async def test_state_expirado_e_recusado():
    membro = await _membro("StateExpirado")
    with google_configurado():
        state = await google.criar_state(membro.id)
        async with Sessao() as s:
            res = await s.execute(
                select(OAuthState).where(OAuthState.member_id == membro.id)
            )
            registro = res.scalar_one()
            registro.expira_em = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()
        assert await google.consumir_state(state) == (None, "state_expirado")


async def test_state_desconhecido_e_recusado():
    with google_configurado():
        assert await google.consumir_state("nunca-existiu") == (None, "state_invalido")
        assert await google.consumir_state("") == (None, "state_invalido")


# ── URL de consentimento ─────────────────────────────────────────────────


async def test_url_de_consentimento_pede_o_minimo_e_leva_o_state():
    membro = await _membro("UrlConsent")
    with google_configurado():
        url = await google.iniciar_conexao(membro.id)
    q = parse_qs(urlparse(url).query)
    assert urlparse(url).netloc == "accounts.google.com"
    assert q["scope"] == ["https://www.googleapis.com/auth/calendar.events"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["response_type"] == ["code"] and q["state"]


async def test_url_de_consentimento_nao_carrega_o_client_secret():
    membro = await _membro("UrlSemSegredo")
    with google_configurado():
        url = await google.iniciar_conexao(membro.id)
        assert "segredo-de-teste" not in url


async def test_sem_configuracao_nao_gera_url():
    membro = await _membro("UrlSemConfig")
    with google_configurado(client_id=""):
        assert await google.iniciar_conexao(membro.id) is None


# ── callback ─────────────────────────────────────────────────────────────


def _api_de(handler) -> GoogleAPI:
    return GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _token_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "ya29.novinho",
            "refresh_token": "1//refresh-novinho",
            "expires_in": 3599,
            "scope": google.ESCOPO,
            "token_type": "Bearer",
        },
    )


async def test_callback_feliz_conecta_o_membro_do_state():
    membro = await _membro("CallbackOk")
    with google_configurado():
        state = await google.criar_state(membro.id)
        ok, motivo = await google.processar_callback(
            code="4/code-do-google", state=state, api=_api_de(_token_ok)
        )
        assert (ok, motivo) == (True, "ok")
        conexao = await google.conexao_de(membro.id)
        assert google.access_token_de(conexao) == "ya29.novinho"
        assert google.refresh_token_de(conexao) == "1//refresh-novinho"
        assert conexao.expira_em is not None


async def test_callback_manda_o_client_secret_no_corpo_e_nao_na_url():
    """Segredo em query string vaza para log de proxy/servidor."""
    vistos = {}

    def handler(request: httpx.Request) -> httpx.Response:
        vistos["url"] = str(request.url)
        vistos["corpo"] = request.content.decode()
        return _token_ok(request)

    membro = await _membro("CallbackCorpo")
    with google_configurado():
        state = await google.criar_state(membro.id)
        await google.processar_callback("4/code", state, api=_api_de(handler))
    assert "segredo-de-teste" not in vistos["url"]
    assert "client_secret=segredo-de-teste" in vistos["corpo"]
    assert "grant_type=authorization_code" in vistos["corpo"]


async def test_callback_com_state_invalido_nao_conecta_ninguem():
    membro = await _membro("CallbackStateRuim")
    with google_configurado():
        ok, motivo = await google.processar_callback(
            "4/code", "state-forjado", api=_api_de(_token_ok)
        )
        assert ok is False and motivo == "state_invalido"
        assert await google.conexao_de(membro.id) is None


async def test_callback_repetido_nao_reabre_a_troca():
    membro = await _membro("CallbackReplay")
    with google_configurado():
        state = await google.criar_state(membro.id)
        assert (await google.processar_callback("4/code", state, api=_api_de(_token_ok)))[0]
        ok, motivo = await google.processar_callback("4/code", state, api=_api_de(_token_ok))
        assert ok is False and motivo == "state_usado"


async def test_callback_de_usuario_que_recusou():
    """O Google devolve ?error=access_denied, sem code. Isso não é bug nosso."""
    membro = await _membro("CallbackNegado")
    with google_configurado():
        state = await google.criar_state(membro.id)
        ok, motivo = await google.processar_callback(
            code=None, state=state, erro="access_denied", api=_api_de(_token_ok)
        )
        assert ok is False and motivo == "negado_pelo_usuario"
        assert await google.conexao_de(membro.id) is None


async def test_callback_sem_code_nao_chama_o_google():
    membro = await _membro("CallbackSemCode")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("não deveria chamar o Google sem code")

    with google_configurado():
        state = await google.criar_state(membro.id)
        ok, motivo = await google.processar_callback(None, state, api=_api_de(handler))
        assert ok is False and motivo == "sem_code"


async def test_callback_com_troca_recusada_pelo_google():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    membro = await _membro("CallbackTrocaRuim")
    with google_configurado():
        state = await google.criar_state(membro.id)
        ok, motivo = await google.processar_callback("4/code", state, api=_api_de(handler))
        assert ok is False and motivo == "troca_falhou"
        assert await google.conexao_de(membro.id) is None


async def test_callback_com_google_fora_do_ar():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    membro = await _membro("CallbackSemRede")
    with google_configurado():
        state = await google.criar_state(membro.id)
        ok, motivo = await google.processar_callback("4/code", state, api=_api_de(handler))
        assert ok is False and motivo == "troca_falhou"


# ── escopo concedido (o Google deixa a pessoa desmarcar caixinhas) ───────


def _token_com_escopo(escopo: str | None):
    """Resposta de token com o `scope` que o Google DE FATO concedeu — que não
    é necessariamente o que pedimos: a tela de consentimento tem caixinha."""

    def handler(request: httpx.Request) -> httpx.Response:
        corpo = {"access_token": "ya29.parcial", "refresh_token": "1//r", "expires_in": 3599}
        if escopo is not None:
            corpo["scope"] = escopo
        return httpx.Response(200, json=corpo)

    return handler


async def test_callback_sem_o_escopo_de_eventos_nao_conecta():
    """Consentimento parcial: sem calendar.events, nada do piloto funciona.
    Guardar a conexão aqui é prometer o que não temos — a falha tem que ser
    na hora, com motivo categórico."""
    membro = await _membro("CallbackEscopoParcial")
    with google_configurado():
        state = await google.criar_state(membro.id)
        ok, motivo = await google.processar_callback(
            "4/code",
            state,
            api=_api_de(_token_com_escopo("https://www.googleapis.com/auth/userinfo.email")),
        )
        assert (ok, motivo) == (False, "escopo_insuficiente")
        assert await google.conexao_de(membro.id) is None


async def test_callback_sem_o_campo_scope_nao_inventa_consentimento():
    """Campo ausente ≠ escopo concedido. Assumir o que pedimos seria inventar
    permissão que ninguém deu."""
    membro = await _membro("CallbackEscopoAusente")
    with google_configurado():
        state = await google.criar_state(membro.id)
        ok, motivo = await google.processar_callback(
            "4/code", state, api=_api_de(_token_com_escopo(None))
        )
        assert (ok, motivo) == (False, "escopo_insuficiente")
        assert await google.conexao_de(membro.id) is None


async def test_depois_do_consentimento_parcial_da_para_tentar_de_novo():
    """Não pode sobrar meia conexão travando o /google."""
    membro = await _membro("CallbackEscopoRetentativa")
    with google_configurado():
        parcial = await google.criar_state(membro.id)
        await google.processar_callback("4/code", parcial, api=_api_de(_token_com_escopo("")))

        segundo = await google.criar_state(membro.id)
        ok, _ = await google.processar_callback("4/code", segundo, api=_api_de(_token_ok))
        assert ok is True
        assert google.access_token_de(await google.conexao_de(membro.id)) == "ya29.novinho"


async def test_escopo_guardado_e_o_que_o_google_concedeu():
    """O que fica no banco é o consentimento REAL, não o que pedimos."""
    membro = await _membro("CallbackEscopoGuardado")
    concedido = f"{google.ESCOPO} https://www.googleapis.com/auth/calendar.readonly"
    with google_configurado():
        state = await google.criar_state(membro.id)
        ok, _ = await google.processar_callback(
            "4/code", state, api=_api_de(_token_com_escopo(concedido))
        )
        assert ok is True
        assert (await google.conexao_de(membro.id)).escopo == concedido


async def test_analytics_do_escopo_insuficiente_tem_motivo_categorico():
    membro = await _membro("AnalyticsEscopo")
    with google_configurado():
        state = await google.criar_state(membro.id)
        await google.processar_callback("4/code", state, api=_api_de(_token_com_escopo(None)))

    falhas = [e for e in await _eventos_de(membro.id) if e.tipo == "google_connection_failed"]
    assert falhas and falhas[-1].payload["motivo"] == "escopo_insuficiente"
    assert "ya29.parcial" not in repr(falhas[-1].payload)


async def test_callback_indisponivel_quando_falta_configuracao():
    membro = await _membro("CallbackSemConfig")
    with google_configurado():
        state = await google.criar_state(membro.id)
    with google_configurado(client_secret=""):
        ok, motivo = await google.processar_callback("4/code", state, api=_api_de(_token_ok))
        assert ok is False and motivo == "indisponivel"


# ── analytics: fato sim, segredo não ─────────────────────────────────────


async def _eventos_de(member_id: int) -> list[ProductEvent]:
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(ProductEvent.member_id == member_id)
        )
        return list(res.scalars().all())


async def test_analytics_registra_o_funil_sem_segredo():
    membro = await _membro("AnalyticsOAuth")
    with google_configurado():
        state = await google.criar_state(membro.id)
        await google.iniciar_conexao(membro.id)
        await google.processar_callback("4/code-secreto", state, api=_api_de(_token_ok))

    eventos = await _eventos_de(membro.id)
    tipos = {e.tipo for e in eventos}
    assert "google_connection_started" in tipos
    assert "google_connection_succeeded" in tipos

    proibidos = ("4/code-secreto", "ya29.novinho", "1//refresh-novinho", state)
    for evento in eventos:
        bruto = repr(evento.payload)
        for segredo in proibidos:
            assert segredo not in bruto, f"{evento.tipo} vazou segredo no payload"


async def test_analytics_de_falha_tem_motivo_categorico():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    membro = await _membro("AnalyticsFalha")
    with google_configurado():
        state = await google.criar_state(membro.id)
        await google.processar_callback("4/code", state, api=_api_de(handler))

    falhas = [e for e in await _eventos_de(membro.id) if e.tipo == "google_connection_failed"]
    assert falhas and falhas[-1].payload["motivo"] == "troca_falhou"


async def test_eventos_do_google_estao_declarados_como_sem_turno():
    """Comando e callback nascem FORA de turno — sem esta declaração, cada um
    vira 'evento órfão' no KPI do dashboard, que tem que ficar em zero."""
    from mordomo.reporting.queries import SEM_TURNO_POR_DESENHO

    for tipo in (
        "google_connection_started",
        "google_connection_succeeded",
        "google_connection_failed",
        "google_test_event_created",
        "google_test_event_failed",
        "google_disconnected",
    ):
        assert tipo in SEM_TURNO_POR_DESENHO


async def test_state_ruim_nao_atribui_a_falha_a_membro_nenhum():
    """Sem state válido não existe membro — e o evento de falha NÃO pode
    inventar um. Atribuir a falha a alguém seria pior que a linha sem dono:
    sujaria o funil de outra pessoa com uma tentativa que não foi dela."""
    with google_configurado():
        ok, motivo = await google.processar_callback("4/code", "state-forjado")
    assert (ok, motivo) == (False, "state_invalido")

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent)
            .where(ProductEvent.tipo == "google_connection_failed")
            .order_by(ProductEvent.id.desc())
        )
        ultimo = res.scalars().first()
    assert ultimo is not None
    assert ultimo.member_id is None
    assert ultimo.payload["motivo"] == "state_invalido"
    # e o state forjado não pode aparecer no analytics
    assert "state-forjado" not in repr(ultimo.payload)


async def test_state_expirado_e_usado_tambem_recusam_no_callback():
    """Os outros dois motivos de state ruim, pelo caminho completo do callback."""
    membro = await _membro("CallbackStateExpirado")
    with google_configurado():
        state = await google.criar_state(membro.id)
        async with Sessao() as s:
            res = await s.execute(
                select(OAuthState).where(OAuthState.member_id == membro.id)
            )
            registro = res.scalar_one()
            registro.expira_em = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()
        assert (await google.processar_callback("4/code", state))[1] == "state_expirado"
        assert await google.conexao_de(membro.id) is None
