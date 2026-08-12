"""Notificações proativas abstraídas (ADR-001): quem chama `notificar()` não
sabe o canal. O registry recebe os adapters ativos; no WhatsApp (fase 3) é o
adapter que decide free-form (janela 24h) vs. template aprovado."""

import logging

from .analytics import emitir
from .channels.contract import ChannelAdapter
from .config import settings
from .identity import identidade_do_membro
from .observability import session_id_de

log = logging.getLogger(__name__)
_adapters: dict[str, ChannelAdapter] = {}


def registrar_adapter(adapter: ChannelAdapter) -> None:
    _adapters[adapter.caps.canal] = adapter


def canais_por_preferencia() -> list[str]:
    """Ordem de tentativa dos canais na proatividade.

    Durante a migração (fase 3) uma pessoa pode ter as DUAS identidades: quem
    já está no WhatsApp deve receber o lembrete lá, não num Telegram que
    ninguém abre — que é exatamente o motivo da migração. Quem ainda não
    vinculou o WhatsApp cai no Telegram sozinho, sem flag nenhuma."""
    preferido = settings.canal_preferido
    return sorted(_adapters, key=lambda canal: canal != preferido)


async def notificar(member_id: int, texto: str) -> bool:
    """Envia proativamente pelo canal preferido em que o membro existe.

    Falha de ENVIO (usuário bloqueou o bot, rede fora) também devolve False —
    nunca exceção. O scheduler conta com isso para marcar o lembrete como
    "falhou" (em vez de deixá-lo "pendente" para sempre com o job já perdido),
    e a curadoria/briefing para seguir para o próximo adulto da lista."""
    for canal in canais_por_preferencia():
        adapter = _adapters[canal]
        ext = await identidade_do_membro(member_id, canal)
        if not ext:
            continue
        try:
            await adapter.notificar(member_id, texto)
        except Exception:
            log.exception("Falha ao notificar membro %s via %s", member_id, canal)
            return False
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
