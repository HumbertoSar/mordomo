"""Montagem do grafo: START → supervisor ──Command──> {lembretes | agenda | END}.

Para visualizar/depurar passo a passo sem canal nenhum: `uv run langgraph dev`
(LangGraph Studio) — ver CLAUDE.md."""

from langgraph.graph import END, START, StateGraph

from ..agents.agenda import no_agenda
from ..agents.lembretes import no_lembretes
from ..agents.supervisor import no_supervisor
from .state import EstadoMordomo


def build_graph(checkpointer=None):
    g = StateGraph(EstadoMordomo)
    g.add_node("supervisor", no_supervisor)
    g.add_node("lembretes", no_lembretes)
    g.add_node("agenda", no_agenda)
    g.add_edge(START, "supervisor")
    g.add_edge("lembretes", END)
    g.add_edge("agenda", END)
    return g.compile(checkpointer=checkpointer)
