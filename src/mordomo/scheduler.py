"""Scheduler de lembretes (APScheduler). O agente não é só request/response —
esta é a parte proativa (ADR na v2 do doc, seção 3, decisão 5).

Jobs vivem em memória e são RECONSTRUÍDOS do banco no boot (fonte de verdade =
tabela reminders). Simples, sem serialização de jobstore, e sobrevive a restart."""

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from .analytics import emitir
from .config import settings
from .db.models import Reminder
from .db.session import Sessao
from .notify import notificar
from .observability import session_id_de

log = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler | None = None


def iniciar() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.tz_familia))
        _scheduler.start()
    return _scheduler


async def _disparar(reminder_id: int) -> None:
    async with Sessao() as s:
        lembrete = await s.get(Reminder, reminder_id)
        if lembrete is None or lembrete.status != "pendente":
            return
        ok = await notificar(lembrete.member_id, f"⏰ Lembrete: {lembrete.texto}")
        lembrete.status = "enviado" if ok else "falhou"
        await s.commit()
    # Sem turn_id: disparo proativo não nasce de uma pergunta. Mas com session_id,
    # para caber na conversa do dia daquele membro.
    await emitir(
        "reminder_fired",
        lembrete.member_id,
        session_id_de(lembrete.member_id),
        texto=lembrete.texto,
        ok=ok,
    )


def agendar(reminder_id: int, quando_utc: datetime) -> None:
    iniciar().add_job(
        _disparar,
        "date",
        run_date=quando_utc,
        args=[reminder_id],
        id=f"lembrete-{reminder_id}",
        replace_existing=True,
        misfire_grace_time=3600,
    )


def cancelar_job(reminder_id: int) -> None:
    try:
        iniciar().remove_job(f"lembrete-{reminder_id}")
    except Exception:  # noqa: BLE001
        log.debug("Job do lembrete %s já não existia (provável disparo prévio)", reminder_id)


async def carregar_pendentes() -> int:
    """No boot: agenda os futuros e dispara os que venceram enquanto estava fora."""
    agora = datetime.now(UTC)
    async with Sessao() as s:
        res = await s.execute(select(Reminder).where(Reminder.status == "pendente"))
        pendentes = list(res.scalars())
    for lembrete in pendentes:
        if lembrete.quando_utc <= agora:
            await _disparar(lembrete.id)
        else:
            agendar(lembrete.id, lembrete.quando_utc)
    log.info("Scheduler: %d lembrete(s) pendente(s) processado(s)", len(pendentes))
    return len(pendentes)
