"""Briefing matinal — o hábito diário que a proatividade compra.

Todo dia às BRIEFING_HORA, cada membro recebe o SEU dia: lembretes dele + a
agenda da família de hoje. Regra anti-spam deliberada: dia vazio = nenhuma
mensagem (mordomo bom não acorda ninguém para dizer que não há nada).

Passa por notify.notificar(), como todo proativo (ADR-001): o canal decide como
entregar — no WhatsApp da fase 3, janela de 24h fechada vira template."""

import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import settings
from .db.models import FamilyEvent, Member, Reminder
from .db.session import Sessao
from .notify import notificar

log = logging.getLogger(__name__)


def _fmt_hora(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def _janela_de_hoje() -> tuple[datetime, datetime, ZoneInfo]:
    tz = ZoneInfo(settings.tz_familia)
    hoje = datetime.now(tz).date()
    inicio = datetime.combine(hoje, time.min, tzinfo=tz)
    return inicio.astimezone(UTC), (inicio + timedelta(days=1)).astimezone(UTC), tz


async def montar_briefing(member_id: int) -> str | None:
    """O dia do membro em texto de mordomo, ou None se não há nada (anti-spam)."""
    inicio, fim, tz = _janela_de_hoje()
    agora = datetime.now(UTC)

    async with Sessao() as s:
        res = await s.execute(
            select(Reminder)
            .where(
                Reminder.member_id == member_id,
                Reminder.status == "pendente",
                Reminder.quando_utc >= agora,   # o que já tocou hoje não volta
                Reminder.quando_utc < fim,
            )
            .order_by(Reminder.quando_utc)
        )
        lembretes = list(res.scalars())
        res = await s.execute(
            select(FamilyEvent)
            .where(FamilyEvent.inicio_utc >= inicio, FamilyEvent.inicio_utc < fim)
            .order_by(FamilyEvent.inicio_utc)
        )
        eventos = list(res.scalars())

    if not lembretes and not eventos:
        return None

    partes = ["Bom dia! ☀️ O dia de hoje:"]
    if eventos:
        partes.append("\nAgenda da família:")
        partes += [
            f"• {_fmt_hora(e.inicio_utc, tz)} — {e.titulo}" + (f" ({e.local})" if e.local else "")
            for e in eventos
        ]
    if lembretes:
        partes.append("\nSeus lembretes:")
        partes += [f"• {_fmt_hora(r.quando_utc, tz)} — {r.texto}" for r in lembretes]
    return "\n".join(partes)


async def enviar_briefings() -> int:
    """Envia a cada membro que tem algo hoje. Devolve quantos foram enviados."""
    async with Sessao() as s:
        membros = list((await s.execute(select(Member))).scalars())

    enviados = 0
    for membro in membros:
        try:
            texto = await montar_briefing(membro.id)
            if texto and await notificar(membro.id, texto):
                enviados += 1
        except Exception:
            log.exception("Briefing falhou para o membro %s", membro.id)
    log.info("Briefing matinal: %d enviado(s) de %d membro(s)", enviados, len(membros))
    return enviados
