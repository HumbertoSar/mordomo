"""Janela de contexto — o freio do crescimento sem teto (ADR-007).

O dashboard mostrou, no primeiro dia de uso real, os tokens de ENTRADA do
supervisor subindo turno a turno: 929 → 1005 → … → 1501 (~72/turno, monótono).
Consequência direta do ADR-003 (thread = membro): `state["messages"]` acumula
para sempre, e cada chamada de LLM recebia a conversa INTEIRA — o roteador para
escolher um destino entre três, o subagente a preço de sonnet.

A janela corta na CHAMADA, não na persistência: o histórico completo continua
no checkpointer (a memória pertence à pessoa — ADR-003 intacto); só o que vai
ao modelo é limitado às últimas N mensagens. Follow-ups como "cancela o
segundo" ou "muda pra sábado" seguem funcionando porque o contexto RECENTE é
justamente o que a janela preserva — garantido por casos multi-turno no eval
de roteamento (evals/datasets/roteamento.json).
"""

from langchain_core.messages import BaseMessage, HumanMessage

from ..config import settings


def janela(mensagens: list[BaseMessage], max_mensagens: int | None = None) -> list[BaseMessage]:
    """Últimas `max_mensagens` mensagens, começando SEMPRE em HumanMessage.

    Começar no meio de um par tool_call/ToolMessage quebra a API dos provedores
    (tool response sem o call correspondente) — por isso, depois do corte,
    avançamos até a primeira HumanMessage. 0 ou None = sem janela (desligado).
    """
    n = settings.contexto_janela_mensagens if max_mensagens is None else max_mensagens
    if not n or len(mensagens) <= n:
        return list(mensagens)

    corte = mensagens[-n:]
    for i, m in enumerate(corte):
        if isinstance(m, HumanMessage):
            return corte[i:]
    # Janela sem nenhuma HumanMessage (só aconteceria com N minúsculo e um loop
    # ReAct longo): melhor a última mensagem do usuário que um corte inválido.
    ultimas_humanas = [m for m in mensagens if isinstance(m, HumanMessage)]
    return ultimas_humanas[-1:] if ultimas_humanas else list(mensagens[-1:])
