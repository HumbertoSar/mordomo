"""Convites de vínculo — /convidar gera código, /vincular consome.

Regras (a primeira permissão por papel DE FATO do projeto):
  - Só membro com papel "adulto" convida. Quem convida decide nome e papel do
    convidado — criança não escolhe ser adulto.
  - Código de uso único, expira em 7 dias, alfabeto sem caracteres ambíguos
    (nada de 0/O nem 1/I/L — vai ser digitado de um celular para outro).
  - Tudo acontece NA BORDA (ADR-003): o LLM nunca participa de identidade.

Cada caminho emite analytics (invite_created / invite_used / invite_rejected):
funil de onboarding também é funil."""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .analytics import emitir
from .db.models import ChannelIdentity, InviteCode, Member
from .db.session import Sessao

# Sem 0/O/1/I/L — o código vai ser lido em voz alta e digitado no celular
_ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
VALIDADE_DIAS = 7
PAPEIS = ("adulto", "crianca")


def _gerar_codigo() -> str:
    return "".join(secrets.choice(_ALFABETO) for _ in range(6))


async def criar_convite(criador: Member, nome: str, papel: str) -> str | None:
    """Devolve o código, ou None se o criador não tem permissão (não é adulto)."""
    if criador.papel != "adulto":
        await emitir("invite_rejected", criador.id, motivo="criador_nao_adulto")
        return None
    if papel not in PAPEIS:
        papel = "adulto"
    async with Sessao() as s:
        convite = InviteCode(
            codigo=_gerar_codigo(),
            nome=nome.strip()[:80],
            papel=papel,
            criado_por=criador.id,
            expira_em=datetime.now(UTC) + timedelta(days=VALIDADE_DIAS),
        )
        s.add(convite)
        await s.commit()
        await s.refresh(convite)
    await emitir("invite_created", criador.id, papel=papel, convite_id=convite.id)
    return convite.codigo


async def usar_convite(codigo: str, canal: str, external_id: str) -> tuple[Member | None, str]:
    """Consome o código e cadastra a identidade. Devolve (membro, motivo).

    Motivos de recusa: ja_cadastrado · codigo_invalido · ja_usado · expirado.
    Nunca detalhamos ao usuário QUAL código existe — só se o dele valeu."""
    async with Sessao() as s:
        # Já é da família? Não desperdiça o código.
        res = await s.execute(
            select(Member)
            .join(ChannelIdentity)
            .where(ChannelIdentity.canal == canal, ChannelIdentity.external_id == str(external_id))
        )
        if (existente := res.scalar_one_or_none()) is not None:
            return existente, "ja_cadastrado"

        res = await s.execute(select(InviteCode).where(InviteCode.codigo == codigo.strip().upper()))
        convite = res.scalar_one_or_none()
        if convite is None:
            await emitir("invite_rejected", motivo="codigo_invalido", canal=canal)
            return None, "codigo_invalido"
        if convite.usado_por is not None:
            await emitir("invite_rejected", motivo="ja_usado", canal=canal)
            return None, "ja_usado"
        expira = convite.expira_em
        if expira.tzinfo is None:  # SQLite devolve naive; o valor foi gravado em UTC
            expira = expira.replace(tzinfo=UTC)
        if expira < datetime.now(UTC):
            await emitir("invite_rejected", motivo="expirado", canal=canal)
            return None, "expirado"

        membro = Member(nome=convite.nome, papel=convite.papel)
        s.add(membro)
        await s.flush()
        s.add(ChannelIdentity(member_id=membro.id, canal=canal, external_id=str(external_id)))
        convite.usado_por = membro.id
        convite.usado_em = datetime.now(UTC)
        await s.commit()
        await s.refresh(membro)

    await emitir("invite_used", membro.id, papel=membro.papel, canal=canal)
    return membro, "ok"
