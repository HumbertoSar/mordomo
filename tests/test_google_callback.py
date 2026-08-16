"""Rota do callback OAuth — a borda HTTP do piloto Google.

Só roda com o extra `whatsapp` instalado (é o mesmo FastAPI): sem ele, pula,
exatamente como o teste do webhook. Nada de rede: o transporte do Google é
injetado (httpx.MockTransport)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from apoio import criar_membro as _membro
from apoio import google_configurado
from mordomo.db.models import OAuthState
from mordomo.db.session import Sessao
from mordomo.integracoes import google
from mordomo.integracoes.google_api import GoogleAPI

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

CODE = "4/code-supersecreto"
ACCESS = "ya29.access-supersecreto"
REFRESH = "1//refresh-supersecreto"


def _cliente(api=None):
    pytest.importorskip("fastapi", reason="extra `whatsapp` não instalado")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mordomo.integracoes.google_callback import criar_router

    app = FastAPI()
    app.include_router(criar_router(api=api))
    return TestClient(app)


def _api_ok() -> GoogleAPI:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "expires_in": 3599,
                "scope": google.ESCOPO,
            },
        )

    return GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_callback_feliz_conecta_e_manda_voltar_para_a_conversa():
    membro = await _membro("RotaOk")
    with google_configurado():
        state = await google.criar_state(membro.id)
        resposta = _cliente(_api_ok()).get(
            "/integracoes/google/callback", params={"code": CODE, "state": state}
        )

        assert resposta.status_code == 200
        assert "text/html" in resposta.headers["content-type"]
        corpo = resposta.text
        # o caminho de volta é a conversa, não uma página que vira produto
        assert "WhatsApp" in corpo or "Telegram" in corpo
        assert "/google_teste" in corpo

        conexao = await google.conexao_de(membro.id)
        assert google.access_token_de(conexao) == ACCESS


async def test_pagina_de_sucesso_nao_exibe_code_state_nem_token():
    """A URL do callback fica no histórico do navegador; a PÁGINA não pode
    somar a isso token nem segredo nenhum."""
    membro = await _membro("RotaSemVazamento")
    with google_configurado():
        state = await google.criar_state(membro.id)
        corpo = _cliente(_api_ok()).get(
            "/integracoes/google/callback", params={"code": CODE, "state": state}
        ).text

    for segredo in (CODE, ACCESS, REFRESH, state):
        assert segredo not in corpo


async def test_callback_com_state_forjado_nao_conecta_e_explica_sem_detalhe():
    membro = await _membro("RotaStateForjado")
    with google_configurado():
        resposta = _cliente(_api_ok()).get(
            "/integracoes/google/callback", params={"code": CODE, "state": "forjado"}
        )

        assert resposta.status_code == 400
        assert await google.conexao_de(membro.id) is None
        # motivo técnico é para o log, não para a tela
        for feio in ("state_invalido", "Traceback", "sqlalchemy", CODE):
            assert feio not in resposta.text


async def test_callback_sem_state_nao_explode():
    """Alguém abrindo a URL na mão (ou um scanner) não pode derrubar a rota."""
    with google_configurado():
        resposta = _cliente(_api_ok()).get("/integracoes/google/callback")
    assert resposta.status_code == 400
    assert "Traceback" not in resposta.text


async def test_callback_de_quem_clicou_em_cancelar():
    membro = await _membro("RotaCancelou")
    with google_configurado():
        state = await google.criar_state(membro.id)
        resposta = _cliente(_api_ok()).get(
            "/integracoes/google/callback",
            params={"error": "access_denied", "state": state},
        )
        assert resposta.status_code == 400
        assert await google.conexao_de(membro.id) is None
    # recusar não é erro do sistema: o texto tem que ser acolhedor
    assert "/google" in resposta.text


async def test_replay_do_callback_nao_reconecta():
    """F5 na página de sucesso: o state já foi queimado."""
    membro = await _membro("RotaReplay")
    with google_configurado():
        state = await google.criar_state(membro.id)
        cliente = _cliente(_api_ok())
        params = {"code": CODE, "state": state}
        assert cliente.get("/integracoes/google/callback", params=params).status_code == 200
        assert cliente.get("/integracoes/google/callback", params=params).status_code == 400


async def test_state_e_marcado_como_usado_no_banco():
    membro = await _membro("RotaStateQueimado")
    with google_configurado():
        state = await google.criar_state(membro.id)
        _cliente(_api_ok()).get(
            "/integracoes/google/callback", params={"code": CODE, "state": state}
        )
    async with Sessao() as s:
        res = await s.execute(select(OAuthState).where(OAuthState.member_id == membro.id))
        assert res.scalar_one().usado_em is not None


async def test_rota_existe_no_app_do_whatsapp():
    """A exigência era 'no MESMO FastAPI já usado pelo WhatsApp' — sem um
    segundo servidor, uma segunda porta e uma segunda entrada no Caddy."""
    pytest.importorskip("fastapi", reason="extra `whatsapp` não instalado")
    import asyncio

    from mordomo.channels.whatsapp_webhook import ROTA, criar_app

    class _Adapter:
        def __init__(self):
            self.fila = asyncio.Queue()

        def enfileirar(self, payload):  # pragma: no cover
            self.fila.put_nowait(payload)

    from fastapi.testclient import TestClient

    cliente = TestClient(criar_app(_Adapter()))
    # As rotas são checadas BATENDO nelas: contar objetos de rota depende de
    # detalhe interno do FastAPI (routers incluídos não expõem `.path`).
    assert cliente.get("/integracoes/google/callback").status_code != 404
    assert cliente.get("/healthz").json()["ok"] is True
    # o webhook do WhatsApp tem que continuar de pé: sem token válido, 403 —
    # o que prova que a rota existe e a validação dela segue mandando
    assert cliente.get(ROTA, params={"hub.mode": "subscribe"}).status_code == 403


async def test_callback_com_integracao_desligada_responde_sem_quebrar():
    membro = await _membro("RotaDesligada")
    with google_configurado():
        state = await google.criar_state(membro.id)
    with google_configurado(client_secret=""):
        resposta = _cliente(_api_ok()).get(
            "/integracoes/google/callback", params={"code": CODE, "state": state}
        )
    assert resposta.status_code == 400
    assert "Traceback" not in resposta.text


async def test_pagina_de_consentimento_parcial_explica_o_que_faltou():
    """Desmarcar a caixinha do calendário na tela do Google não pode virar
    página genérica: a pessoa precisa saber que tem que aceitar a permissão."""
    membro = await _membro("RotaEscopoParcial")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3599}
        )

    with google_configurado():
        state = await google.criar_state(membro.id)
        resposta = _cliente(
            GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        ).get("/integracoes/google/callback", params={"code": CODE, "state": state})

        assert resposta.status_code == 400
        assert await google.conexao_de(membro.id) is None

    minusculo = resposta.text.lower()
    assert "permiss" in minusculo or "agenda" in minusculo
    assert "/google" in resposta.text
    for feio in ("escopo_insuficiente", "Traceback", ACCESS, REFRESH):
        assert feio not in resposta.text


async def test_callback_nao_e_indexado_por_buscador():
    """A página carrega o resultado de um fluxo de conta — não deve virar
    resultado de busca."""
    membro = await _membro("RotaNoIndex")
    with google_configurado():
        state = await google.criar_state(membro.id)
        resposta = _cliente(_api_ok()).get(
            "/integracoes/google/callback", params={"code": CODE, "state": state}
        )
    assert "noindex" in resposta.text.lower()


async def test_token_guardado_pela_rota_fica_cifrado():
    """Ponta a ponta pela borda HTTP: nada em claro no banco."""
    membro = await _membro("RotaCifrada")
    with google_configurado():
        state = await google.criar_state(membro.id)
        _cliente(_api_ok()).get(
            "/integracoes/google/callback", params={"code": CODE, "state": state}
        )
        conexao = await google.conexao_de(membro.id)
        assert ACCESS not in conexao.access_token_cifrado
        assert REFRESH not in (conexao.refresh_token_cifrado or "")
        assert conexao.expira_em > datetime.now(UTC) + timedelta(minutes=50)
