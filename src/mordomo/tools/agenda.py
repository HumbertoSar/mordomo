"""Tools do subagente Agenda.

MVP: agenda COMPARTILHADA da família numa tabela própria — roda no dia 1 sem
OAuth. Fase 2 (ADR-004): trocar por Google Calendar, de propósito de DUAS
formas para comparar no portfólio: (a) tool nativa escrita à mão (a dor do
OAuth ensina) e (b) MCP server pronto via langchain-mcp-adapters."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from ..analytics import emitir
from ..config import settings
from ..db.models import FamilyEvent
from ..db.session import Sessao
from .datas import resolver_data


def _fmt(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(settings.tz_familia)).strftime("%a %d/%m às %H:%M")


@tool
async def criar_evento(titulo: str, quando: str, local: str | None, config: RunnableConfig) -> str:
    """Cria um compromisso na agenda da família.

    Args:
        titulo: ex. "consulta pediatra do João".
        quando: expressão de tempo em português, exatamente como o usuário disse.
        local: opcional (ex. "clínica do centro").
    """
    member_id = config["configurable"]["member_id"]
    await emitir("tool_called", member_id, tool="criar_evento", quando=quando)
    dt = resolver_data(quando)
    if dt is None:
        await emitir(
            "tool_result", member_id, tool="criar_evento", ok=False, motivo="data_nao_entendida"
        )
        return (
            f"NÃO ENTENDI a expressão de tempo '{quando}'. "
            "Pergunte ao usuário a data e a hora exatas (não invente!)."
        )
    if dt <= datetime.now(UTC).astimezone(dt.tzinfo):
        await emitir(
            "tool_result", member_id, tool="criar_evento", ok=False, motivo="data_no_passado"
        )
        return f"'{quando}' resolveu para {_fmt(dt)}, que já passou. Confirme a data com o usuário."
    async with Sessao() as s:
        ev = FamilyEvent(
            titulo=titulo, inicio_utc=dt.astimezone(UTC), local=local, criado_por=member_id
        )
        s.add(ev)
        await s.commit()
        await s.refresh(ev)
    await emitir("tool_result", member_id, tool="criar_evento", ok=True, evento_id=ev.id)
    sufixo = f" ({local})" if local else ""
    return f"Evento criado: {titulo} — {_fmt(dt)}{sufixo}"


@tool
async def listar_agenda(dias: int, config: RunnableConfig) -> str:
    """Lista os compromissos da família nos próximos N dias (use 1 para hoje, 7 para a semana)."""
    member_id = config["configurable"]["member_id"]
    await emitir("tool_called", member_id, tool="listar_agenda", dias=dias)
    agora = datetime.now(UTC)
    ate = agora + timedelta(days=max(1, dias))
    async with Sessao() as s:
        res = await s.execute(
            select(FamilyEvent)
            .where(FamilyEvent.inicio_utc >= agora, FamilyEvent.inicio_utc <= ate)
            .order_by(FamilyEvent.inicio_utc)
        )
        eventos = list(res.scalars())
    if not eventos:
        return f"Agenda livre nos próximos {dias} dia(s)."
    return "\n".join(
        f"• {_fmt(e.inicio_utc)} — {e.titulo}" + (f" ({e.local})" if e.local else "")
        for e in eventos
    )


TOOLS_AGENDA = [criar_evento, listar_agenda]
