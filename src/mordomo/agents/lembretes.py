"""Subagente Lembretes — ReAct (LLM + tools em loop) sem checkpointer próprio:
o histórico mora no grafo pai; aqui cada chamada é stateless."""

from ..analytics import emitir_de, uso_de
from ..config import settings
from ..core.contexto import janela
from ..core.llm import chat_model, criar_agente
from ..core.state import EstadoMordomo
from ..tools.lembretes import TOOLS_LEMBRETES

PROMPT_LEMBRETES = """Você é o especialista em LEMBRETES do Mordomo da Família.

Regras:
- Ao criar, passe para a tool a expressão de tempo EXATAMENTE como o usuário
  disse (ex.: "amanhã às 8h") — quem resolve a data é a tool, não você.
- Se a tool disser que NÃO ENTENDEU a data ou que ela está no passado,
  pergunte ao usuário a data/hora exata. NUNCA invente data.
- Se faltar a hora, assuma nada: pergunte ("de manhã? que horas?").
- Respostas curtas, tom de mordomo simpático, formato WhatsApp (sem markdown
  pesado, sem listas longas).
"""

_agente = None


def agente_lembretes():
    global _agente
    if _agente is None:
        _agente = criar_agente(chat_model(settings.model_agente), TOOLS_LEMBRETES, PROMPT_LEMBRETES)
    return _agente


async def no_lembretes(state: EstadoMordomo, config) -> dict:
    # Janela (ADR-007): o subagente herda o histórico via grafo pai; ao LLM vai
    # só o recente. O checkpointer segue guardando tudo.
    entrada = janela(state["messages"])
    anteriores = len(entrada)
    resultado = await agente_lembretes().ainvoke({"messages": entrada}, config)

    # Só as mensagens que ESTE nó produziu: as antigas já carregam o
    # usage_metadata do turno em que nasceram e seriam contadas duas vezes.
    novas = resultado["messages"][anteriores:] or resultado["messages"][-1:]
    if (uso := uso_de(novas)) is not None:
        await emitir_de(config, "llm_usage", no="lembretes", **uso)

    return {"messages": [resultado["messages"][-1]]}
