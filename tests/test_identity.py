"""Resolução de identidade (canal, external_id) → membro."""

from mordomo.db.models import ChannelIdentity, Member
from mordomo.db.session import Sessao
from mordomo.identity import identidade_do_membro, resolver_membro


async def test_resolve_membro_cadastrado():
    async with Sessao() as s:
        m = Member(nome="Teste", papel="adulto")
        s.add(m)
        await s.flush()
        s.add(ChannelIdentity(member_id=m.id, canal="telegram", external_id="999001"))
        await s.commit()
        member_id = m.id

    membro = await resolver_membro("telegram", "999001")
    assert membro is not None and membro.id == member_id
    assert await identidade_do_membro(member_id, "telegram") == "999001"


async def test_desconhecido_devolve_none():
    assert await resolver_membro("telegram", "000000") is None
