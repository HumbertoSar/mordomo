"""Notificações proativas abstraídas (ADR-001): quem chama `notificar()` não
sabe o canal. O registry recebe os adapters ativos; no WhatsApp (fase 3) é o
adapter que decide free-form (janela 24h) vs. template aprovado."""

import logging

from .analytics import emitir
from .channels.contract import ChannelAdapter
from .identity import identidade_do_membro
from .observability import session_id_de

log = logging.getLogger(__name__)
_adapters: dict[str, ChannelAdapter] = {}


def registrar_adapter(adapter: ChannelAdapter) -> None:
    _adapters[adapter.caps.canal] = adapter


async def notificar(member_id: int, texto: str) -> bool:
    """Envia proativamente pelo primeiro canal em que o membro existe."""
    for canal, adapter in _adapters.items():
        ext = await identidade_do_membro(member_id, canal)
        if ext:
            await adapter.notificar(member_id, texto)
            await emitir(
                "proactive_sent",
                member_id,
                session_id_de(member_id),
                canal=canal,
                tamanho=len(texto),
            )
            return True
    log.warning("Membro %s sem canal para notificação proativa", member_id)
    return False
