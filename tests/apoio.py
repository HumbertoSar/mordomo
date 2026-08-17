"""Helpers compartilhados dos testes — antes copiados em 4 arquivos.

`criar_membro` grava um Member real no banco de teste; `cfg_de` monta o
config no MESMO formato do config_invocacao de produção (member/session/turn
no configurable — ADR-003)."""

import contextlib
from datetime import UTC, datetime, timedelta

import httpx

from mordomo.db.models import Member
from mordomo.db.session import Sessao


async def criar_membro(nome: str, papel: str = "adulto") -> Member:
    async with Sessao() as s:
        m = Member(nome=nome, papel=papel)
        s.add(m)
        await s.commit()
        await s.refresh(m)
        return m


def cfg_de(membro: Member, turn: str) -> dict:
    return {
        "configurable": {
            "member_id": membro.id,
            "member_papel": membro.papel,
            "session_id": f"{membro.id}:teste",
            "turn_id": turn,
        }
    }


@contextlib.contextmanager
def google_configurado(**mudancas):
    """Liga a integração Google só DURANTE o teste.

    `settings` é singleton de processo: sem restaurar no fim, um teste que
    liga a integração contamina os seguintes. A chave Fernet é gerada na hora
    (nunca a real, nunca do .env) — cada uso gera uma chave NOVA, que é
    justamente o que permite testar rotação de chave."""
    from mordomo.config import settings
    from mordomo.integracoes.cripto import gerar_chave

    valores = {
        "google_client_id": "client-de-teste.apps.googleusercontent.com",
        "google_client_secret": "segredo-de-teste",
        "google_redirect_uri": "https://exemplo.test/integracoes/google/callback",
        "google_token_key": gerar_chave(),
    }
    for curto, valor in mudancas.items():
        valores[curto if curto.startswith("google_") else f"google_{curto}"] = valor

    anteriores = {campo: getattr(settings, campo) for campo in valores}
    for campo, valor in valores.items():
        setattr(settings, campo, valor)
    try:
        yield settings
    finally:
        for campo, valor in anteriores.items():
            setattr(settings, campo, valor)


# ── Google falso (MockTransport) — sem rede, sem chave (regra nº 7) ──────


def gravador(respostas=None):
    """Handler httpx que GUARDA cada requisição; por padrão responde 200.

    `respostas` é uma fila de funções (request → Response) para o teste desenhar
    a conversa com o Google passo a passo; esgotada, volta o padrão."""
    chamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        if respostas:
            return respostas.pop(0)(request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(
            200, json={"id": "evt-1", "htmlLink": "https://calendar.google.com/event?eid=xyz"}
        )

    handler.chamadas = chamadas
    return handler


def injetar_google(monkeypatch, handler) -> None:
    """Faz `google.GoogleAPI()` nascer falando com o MockTransport.

    As tools não recebem `api` (os argumentos delas são do LLM), então a
    injeção acontece no ponto onde o cliente é criado."""
    from mordomo.integracoes import google
    from mordomo.integracoes.google_api import GoogleAPI

    monkeypatch.setattr(
        google,
        "GoogleAPI",
        lambda: GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
    )


async def membro_conectado(nome: str, *, expira_em_min: int = 60) -> Member:
    """Membro com conexão Google válida (tokens de mentira, cifrados na hora)."""
    from mordomo.integracoes import google

    membro = await criar_membro(nome)
    await google.salvar_conexao(
        membro.id,
        access_token="access-valido",
        refresh_token="refresh-valido",
        expira_em=datetime.now(UTC) + timedelta(minutes=expira_em_min),
        escopo=google.ESCOPO,
    )
    return membro
