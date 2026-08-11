"""Helpers compartilhados dos testes — antes copiados em 4 arquivos.

`criar_membro` grava um Member real no banco de teste; `cfg_de` monta o
config no MESMO formato do config_invocacao de produção (member/session/turn
no configurable — ADR-003)."""

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
