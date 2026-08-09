"""Supervisor ("o Mordomo") — roteador construído à mão (ADR-002): structured
output decide o destino e `Command(goto=...)` faz o handoff. Mais didático que
o pacote langgraph-supervisor; a acurácia deste roteamento é o eval nº 2
(evals/datasets/roteamento.json)."""

from typing import Literal

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import Command
from pydantic import BaseModel, Field

from ..analytics import emitir
from ..config import settings
from ..core.llm import chat_model
from ..core.state import EstadoMordomo
from ..observability import session_id_de

PROMPT_SUPERVISOR = """Você é o Mordomo da Família: educado, caloroso, direto \
e brasileiro. Você conversa pelo Telegram/WhatsApp, então respostas são CURTAS.

Sua única função neste passo é DECIDIR o destino da mensagem:

- "lembretes": criar/listar/cancelar lembretes ("me lembra…", "que lembretes eu tenho?")
- "agenda": compromissos da família ("marca consulta…", "o que temos sábado?")
- "responder": cumprimentos, agradecimentos, small talk ou perguntas gerais que
  você mesmo responde em 1-2 frases (preencha o campo `resposta`).

Regras:
- Ambiguidade real ("marca aí" sem contexto): destino "responder" com uma
  pergunta curta de esclarecimento.
- Pedidos fora do escopo atual (e-mail, filmes, tarefas): destino "responder",
  explique com bom humor que essa mordomia chega em breve.
- Está falando com {nome} (papel: {papel}).
"""


class Decisao(BaseModel):
    """Decisão de roteamento do supervisor."""

    destino: Literal["lembretes", "agenda", "responder"] = Field(
        description="Subagente de destino, ou 'responder' para responder diretamente"
    )
    resposta: str | None = Field(
        default=None, description="Resposta curta ao usuário quando destino='responder'"
    )


async def no_supervisor(
    state: EstadoMordomo,
) -> Command[Literal["lembretes", "agenda", "__end__"]]:
    modelo = chat_model(settings.model_supervisor).with_structured_output(Decisao)
    sistema = SystemMessage(
        PROMPT_SUPERVISOR.format(nome=state.get("member_nome", "?"), papel=state.get("member_papel", "?"))
    )
    decisao: Decisao = await modelo.ainvoke([sistema, *state["messages"]])

    await emitir(
        "orchestrator_decision",
        state.get("member_id"),
        session_id_de(state.get("member_id", 0)),
        destino=decisao.destino,
    )

    if decisao.destino == "responder":
        return Command(
            goto=END,
            update={"messages": [AIMessage(decisao.resposta or "Às ordens!")]},
        )
    return Command(goto=decisao.destino)
