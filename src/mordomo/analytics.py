"""Emissor de eventos de produto (a "gestão à vista" começa aqui).

Cada evento é uma linha em product_events. Convenções de tipo (alinhadas ao
doc `gestao-a-vista-agente-whatsapp.md`):

  message_received · orchestrator_decision · tool_called · tool_result ·
  message_sent · reminder_created · reminder_fired · unknown_user

Falha de analytics NUNCA derruba a conversa (try/except deliberado)."""

import logging

from .db.models import ProductEvent
from .db.session import Sessao

log = logging.getLogger(__name__)


async def emitir(
    tipo: str,
    member_id: int | None = None,
    session_id: str | None = None,
    **payload,
) -> None:
    try:
        async with Sessao() as s:
            s.add(ProductEvent(tipo=tipo, member_id=member_id, session_id=session_id, payload=payload))
            await s.commit()
    except Exception:
        log.exception("Falha ao emitir evento de produto %s (conversa segue normal)", tipo)
