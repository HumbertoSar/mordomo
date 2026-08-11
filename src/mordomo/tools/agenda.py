"""Tools do subagente Agenda.

MVP: agenda COMPARTILHADA da família numa tabela própria — roda no dia 1 sem
OAuth. Fase 2 (ADR-004): trocar por Google Calendar, de propósito de DUAS
formas para comparar no portfólio: (a) tool nativa escrita à mão (a dor do
OAuth ensina) e (b) MCP server pronto via langchain-mcp-adapters."""

from datetime import UTC, datetime, timedelta
from functools import partial

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from ..analytics import emitir_de
from ..db.models import FamilyEvent
from ..db.session import Sessao
from ._comum import fmt_data, resolver_ou_instruir

_fmt = partial(fmt_data, dia_semana=True)  # agenda mostra o dia da semana


@tool
async def criar_evento(titulo: str, quando: str, local: str | None, config: RunnableConfig) -> str:
    """Cria um compromisso na agenda da família.

    Args:
        titulo: ex. "consulta pediatra do João".
        quando: expressão de tempo em português, exatamente como o usuário disse.
        local: opcional (ex. "clínica do centro").
    """
    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="criar_evento", quando=quando)
    dt, instrucao = await resolver_ou_instruir(quando, config, "criar_evento")
    if instrucao:
        return instrucao
    async with Sessao() as s:
        ev = FamilyEvent(
            titulo=titulo, inicio_utc=dt.astimezone(UTC), local=local, criado_por=member_id
        )
        s.add(ev)
        await s.commit()
        await s.refresh(ev)
    await emitir_de(config, "tool_result", tool="criar_evento", ok=True, evento_id=ev.id)
    sufixo = f" ({local})" if local else ""
    return f"Evento criado: {titulo} — {_fmt(dt)}{sufixo}"


@tool
async def listar_agenda(dias: int, config: RunnableConfig) -> str:
    """Lista os compromissos da família nos próximos N dias (use 1 para hoje, 7 para a semana)."""
    # Sem member_id aqui de propósito: a agenda é COMPARTILHADA (ADR-003), e
    # quem pediu já entra no evento via emitir_de.
    dias = max(1, dias)  # normaliza ANTES: janela e resposta contam o mesmo nº
    await emitir_de(config, "tool_called", tool="listar_agenda", dias=dias)
    agora = datetime.now(UTC)
    ate = agora + timedelta(days=dias)
    async with Sessao() as s:
        res = await s.execute(
            select(FamilyEvent)
            .where(FamilyEvent.inicio_utc >= agora, FamilyEvent.inicio_utc <= ate)
            .order_by(FamilyEvent.inicio_utc)
        )
        eventos = list(res.scalars())
    await emitir_de(config, "tool_result", tool="listar_agenda", ok=True, n=len(eventos))
    if not eventos:
        return f"Agenda livre nos próximos {dias} dia(s)."
    return "\n".join(
        f"• {_fmt(e.inicio_utc)} — {e.titulo}" + (f" ({e.local})" if e.local else "")
        for e in eventos
    )


# Tool nova que GRAVA algo? Adicione em core/efeitos.py::TOOLS_MUTANTES,
# senão o retry do pipeline pode executá-la duas vezes.
TOOLS_AGENDA = [criar_evento, listar_agenda]
