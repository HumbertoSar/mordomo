"""Pipeline: InboundMessage → grafo → OutboundMessage(s).

Aqui nascem o trace (Langfuse) e os eventos de produto da conversa — é o
"gateway" lógico entre canal e agente. Aqui também nasce o `turn_id`: um
identificador por PERGUNTA, que todo evento do turno carrega e que amarra
recebi → roteei → chamei tool → gastei tokens → respondi."""

import logging
import time
import uuid

from langchain_core.messages import HumanMessage

from ..analytics import emitir_de
from ..channels.contract import InboundMessage, OutboundMessage
from ..db.models import Member
from ..observability import config_invocacao

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
) -> tuple[str, list[OutboundMessage]]:
    """Devolve (turn_id, respostas).

    O turn_id volta para o adapter porque `message_sent` é fato de CANAL — quem
    sabe se a mensagem saiu de verdade é ele — mas precisa entrar no mesmo funil
    do resto do turno. O adapter recebe um id opaco; não sabe como foi gerado."""
    turn_id = uuid.uuid4().hex[:12]
    cfg = config_invocacao(membro.id, membro.nome, membro.papel, turn_id)

    await emitir_de(
        cfg,
        "message_received",
        canal=inbound.canal,
        veio_de_audio=inbound.veio_de_audio,
        tamanho=len(inbound.texto),
    )

    entrada = {
        "messages": [HumanMessage(inbound.texto)],
        "member_id": membro.id,
        "member_nome": membro.nome,
        "member_papel": membro.papel,
    }

    inicio = time.perf_counter()
    ok = True
    try:
        resultado = await grafo.ainvoke(entrada, cfg)
        texto = _texto_de(resultado["messages"][-1].content) or "…"
    except Exception:
        ok = False
        log.exception("Erro no grafo (membro %s, turno %s)", membro.id, turn_id)
        await emitir_de(cfg, "error", onde="grafo")
        texto = "Ops, tropecei aqui do meu lado. 😅 Pode repetir, por favor?"

    # O evento que sustenta quase todo o dashboard: uma linha por turno, com o
    # tempo que o usuário REALMENTE esperou — não o tempo de uma chamada de LLM.
    await emitir_de(
        cfg,
        "turn_completed",
        ok=ok,
        latencia_ms=round((time.perf_counter() - inicio) * 1000),
        tamanho_resposta=len(texto),
    )

    return turn_id, [OutboundMessage(texto=texto)]
