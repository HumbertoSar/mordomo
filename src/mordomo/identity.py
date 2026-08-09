"""Resolução de identidade: (canal, external_id) → Member.

MVP: família pré-cadastrada via scripts/seed_familia.py; desconhecidos recebem
recusa educada (também é o guardrail de acesso). Fase 2: fluxo /vincular com
código de convite."""

from sqlalchemy import select

from .db.models import ChannelIdentity, Member
from .db.session import Sessao


async def resolver_membro(canal: str, external_id: str) -> Member | None:
    async with Sessao() as s:
        res = await s.execute(
            select(Member)
            .join(ChannelIdentity)
            .where(ChannelIdentity.canal == canal, ChannelIdentity.external_id == str(external_id))
        )
        return res.scalar_one_or_none()


async def identidade_do_membro(member_id: int, canal: str) -> str | None:
    """external_id (ex.: chat_id do Telegram) para mensagens proativas."""
    async with Sessao() as s:
        res = await s.execute(
            select(ChannelIdentity.external_id).where(
                ChannelIdentity.member_id == member_id, ChannelIdentity.canal == canal
            )
        )
        row = res.first()
        return row[0] if row else None
