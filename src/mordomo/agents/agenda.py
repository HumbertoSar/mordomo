"""Subagente Agenda — compromissos da família (agenda compartilhada)."""

from ..analytics import emitir_de, uso_de
from ..config import settings
from ..core.contexto import janela
from ..core.llm import chat_model, criar_agente
from ..core.state import EstadoMordomo
from ..tools.agenda import TOOLS_AGENDA

PROMPT_AGENDA = """Você é o especialista em AGENDA do Mordomo da Família.
A agenda é COMPARTILHADA: eventos criados valem para a família toda.

Regras:
- Ao criar evento, passe a expressão de tempo EXATAMENTE como o usuário disse;
  quem resolve a data é a tool. Se a tool não entender, pergunte — nunca invente.
- "o que temos hoje?" → listar_agenda com dias=1; "essa semana" → dias=7.
- NUNCA afirme um compromisso que não veio da tool (não alucine agenda!).
- Respostas curtas, tom de mordomo simpático, formato WhatsApp.
"""

_agente = None


def agente_agenda():
    global _agente
    if _agente is None:
        _agente = criar_agente(chat_model(settings.model_agente), TOOLS_AGENDA, PROMPT_AGENDA)
    return _agente


async def no_agenda(state: EstadoMordomo, config) -> dict:
    # Janela (ADR-007) — ver nota em agents/lembretes.py.
    entrada = janela(state["messages"])
    anteriores = len(entrada)
    resultado = await agente_agenda().ainvoke({"messages": entrada}, config)

    # Ver nota em agents/lembretes.py: contar só o que este nó gerou.
    novas = resultado["messages"][anteriores:] or resultado["messages"][-1:]
    if (uso := uso_de(novas)) is not None:
        await emitir_de(config, "llm_usage", no="agenda", **uso)

    return {"messages": [resultado["messages"][-1]]}
