"""Nó de pedidos de funcionalidade — DETERMINÍSTICO, sem LLM próprio.

O supervisor já decidiu que a mensagem é um pedido e já extraiu o título
(campo `resposta` da decisão vira `pedido_titulo` no estado). Este nó só:
registra o evento, tenta abrir a issue e agradece. Gastar uma chamada de
subagente para isso seria pagar sonnet por um formulário."""

from langchain_core.messages import AIMessage, HumanMessage

from ..analytics import emitir_de
from ..core.state import EstadoMordomo
from ..github_issues import criar_issue


async def no_pedidos(state: EstadoMordomo, config) -> dict:
    ultima_humana = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
    )
    pedido = str(ultima_humana.content) if ultima_humana else "?"
    titulo = state.get("pedido_titulo") or pedido[:80]
    autor = state.get("member_nome", "?")
    papel = state.get("member_papel", "?")
    categoria = state.get("pedido_tipo") or "funcionalidade"

    # O evento vem ANTES da issue: mesmo sem token/GitHub fora do ar, o pedido
    # fica registrado e aparece no funil.
    await emitir_de(config, "feature_requested", titulo=titulo[:120], categoria=categoria)

    url = await criar_issue(titulo, pedido, autor, papel, categoria)
    await emitir_de(config, "feature_issue_created", ok=url is not None, categoria=categoria)

    if categoria == "problema":
        resposta = (
            f"Sinto muito pelo tropeço, {autor}! 🙇 Registrei o problema "
            f"('{titulo}') para o mordomo-chefe investigar. Obrigado por avisar."
        )
    else:
        resposta = (
            f"Anotado, {autor}! 📋 Registrei seu pedido ('{titulo}') para o "
            "mordomo-chefe avaliar. Se sair do papel, você fica sabendo por aqui."
        )
    return {"messages": [AIMessage(resposta)]}
