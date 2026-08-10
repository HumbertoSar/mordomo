"""Janela de contexto (ADR-007) — cortes válidos, sem órfãos de tool call."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mordomo.core.contexto import janela


def _conversa(turnos: int) -> list:
    msgs = []
    for i in range(turnos):
        msgs.append(HumanMessage(f"pergunta {i}"))
        msgs.append(AIMessage(f"resposta {i}"))
    return msgs


def test_conversa_curta_passa_inteira():
    msgs = _conversa(2)
    assert janela(msgs, 8) == msgs


def test_corta_e_comeca_em_mensagem_humana():
    msgs = _conversa(10)  # 20 mensagens
    corte = janela(msgs, 8)
    assert len(corte) <= 8
    assert isinstance(corte[0], HumanMessage)
    assert corte[-1] is msgs[-1]  # o fim da conversa é preservado


def test_nao_deixa_tool_message_orfa():
    """Cortar entre o tool_call e a ToolMessage quebra a API do provedor."""
    ai_com_tool = AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": "c1"}])
    msgs = [
        HumanMessage("antiga 1"), AIMessage("r1"),
        HumanMessage("antiga 2"), AIMessage("r2"),
        HumanMessage("cria lembrete"),
        ai_com_tool,
        ToolMessage("ok", tool_call_id="c1"),
        AIMessage("criado!"),
    ]
    # Janela de 4 cairia em [ai_com_tool, ToolMessage, ...] — inválido.
    corte = janela(msgs, 4)
    assert isinstance(corte[0], HumanMessage)
    assert not isinstance(corte[0], ToolMessage)


def test_zero_desliga_a_janela():
    msgs = _conversa(30)
    assert janela(msgs, 0) == msgs


def test_janela_minuscula_sem_humana_cai_na_ultima_pergunta():
    ai_com_tool = AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": "c1"}])
    msgs = [
        HumanMessage("pergunta"),
        ai_com_tool,
        ToolMessage("ok", tool_call_id="c1"),
        AIMessage("feito"),
    ]
    corte = janela(msgs, 2)  # só [ToolMessage, AIMessage] — nenhuma humana
    assert len(corte) == 1
    assert isinstance(corte[0], HumanMessage)
