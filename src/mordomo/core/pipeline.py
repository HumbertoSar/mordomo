"""Pipeline: InboundMessage → grafo → OutboundMessage(s).

Aqui nascem o trace (Langfuse) e os eventos de produto da conversa — é o
"gateway" lógico entre canal e agente."""

import logging

from langchain_core.messages import HumanMessage

from ..analytics import emitir
from ..channels.contract import InboundMessage, OutboundMessage
from ..db.models import Member
from ..observability import config_invocacao, session_id_de

log = logging.getLogger(__name__)


def _texto_de(conteudo) -> str:
    """Conteúdo da AIMessage pode ser str ou lista de blocos, conforme o modelo."""
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        partes = [b.get("text", "") if isinstance(b, dict) else str(b) for b in conteudo]
        return "".join(partes).strip()
    return str(conteudo)


async def processar_entrada(
    membro: Member, inbound: InboundMessage, grafo
) -> list[OutboundMessage]:
    session_id = session_id_de(membro.id)
    await emitir(
        "message_received",
        membro.id,
        session_id,
        canal=inbound.canal,
        veio_de_audio=inbound.veio_de_audio,
        tamanho=len(inbound.texto),
    )

    cfg = config_invocacao(membro.id, membro.nome, membro.papel)
    entrada = {
        "messages": [HumanMessage(inbound.texto)],
        "member_id": membro.id,
        "member_nome": membro.nome,
        "member_papel": membro.papel,
    }

    try:
        resultado = await grafo.ainvoke(entrada, cfg)
        texto = _texto_de(resultado["messages"][-1].content) or "…"
    except Exception:
        log.exception("Erro no grafo (membro %s)", membro.id)
        await emitir("error", membro.id, session_id, onde="grafo")
        texto = "Ops, tropecei aqui do meu lado. 😅 Pode repetir, por favor?"

    return [OutboundMessage(texto=texto)]
