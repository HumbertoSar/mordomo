"""Helpers compartilhados dos testes — antes copiados em 4 arquivos.

`criar_membro` grava um Member real no banco de teste; `cfg_de` monta o
config no MESMO formato do config_invocacao de produção (member/session/turn
no configurable — ADR-003)."""

import contextlib

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
