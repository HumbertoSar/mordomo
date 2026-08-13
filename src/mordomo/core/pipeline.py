"""Pipeline: InboundMessage → grafo → OutboundMessage(s).

Aqui nascem o trace (Langfuse) e os eventos de produto da conversa — é o
"gateway" lógico entre canal e agente. Aqui também nasce o `turn_id`: um
identificador por PERGUNTA, que todo evento do turno carrega e que amarra
recebi → roteei → chamei tool → gastei tokens → respondi."""

import asyncio
import logging
import time
import uuid

from langchain_core.messages import HumanMessage

from ..analytics import emitir_de
from ..channels.contract import InboundMessage, OutboundMessage
from ..db.models import Member
from ..observability import config_invocacao
from . import efeitos
from .anexos import coletar

log = logging.getLogger(__name__)


# Um lock por thread do checkpointer (membro ou grupo). O debounce do adapter
# só cancela o flush enquanto ele DORME: mensagem que chega durante os 3-10s
# do turno criaria uma segunda invocação do grafo na MESMA thread — os dois
# turnos leriam o mesmo checkpoint e o último a escrever apagaria o outro do
# histórico. Cresce com o nº de threads ativas (família + grupos): dezenas.
_locks_por_thread: dict[str, asyncio.Lock] = {}


def _lock_da_thread(thread_id: str) -> asyncio.Lock:
    return _locks_por_thread.setdefault(thread_id, asyncio.Lock())


async def _turno_teve_efeito(turn_id: str) -> bool:
    """Alguma tool MUTANTE concluiu ok neste turno?

    Memória primeiro (core/efeitos.py — não depende do banco: se o banco
    piscou e o tool_result se perdeu, é ela que impede o retry de duplicar o
    lembrete); o funil fica de cinto e suspensório."""
    if efeitos.houve_efeito(turn_id):
        return True

    from sqlalchemy import func, select

    from ..db.models import ProductEvent
    from ..db.session import Sessao

    try:
        async with Sessao() as s:
            res = await s.execute(
                select(func.count()).where(
                    ProductEvent.turn_id == turn_id,
                    ProductEvent.tipo == "tool_result",
                    ProductEvent.payload["ok"].as_boolean().is_(True),
                    ProductEvent.payload["tool"].as_string().in_(efeitos.TOOLS_MUTANTES),
                )
            )
            return res.scalar_one() > 0
    except Exception:
        log.exception("Falha ao checar efeito do turno %s", turn_id)
        return True


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
    cfg = config_invocacao(
        membro.id, membro.nome, membro.papel, turn_id, inbound.grupo_id, canal=inbound.canal
    )

    entrada = {
        "messages": [HumanMessage(inbound.texto)],
        "member_id": membro.id,
        "member_nome": membro.nome,
        "member_papel": membro.papel,
    }

    # O perf_counter parte de ANTES do lock: se este turno esperou o anterior
    # terminar, a espera é latência que o usuário sentiu — tem que ser medida.
    inicio = time.perf_counter()
    ok = True
    async with _lock_da_thread(cfg["configurable"]["thread_id"]):
        # Quanto deste turno foi FILA (esperando o turno anterior da mesma
        # thread soltar o lock). Vai separado no turn_completed para o
        # dashboard distinguir "o LLM foi lento" de "as mensagens se
        # atropelaram" — a latência total não conta essa diferença.
        espera_fila_ms = round((time.perf_counter() - inicio) * 1000)
        await emitir_de(
            cfg,
            "message_received",
            canal=inbound.canal,
            veio_de_audio=inbound.veio_de_audio,
            tamanho=len(inbound.texto),
        )

        for tentativa in (1, 2):
            try:
                resultado = await grafo.ainvoke(entrada, cfg)
                texto = _texto_de(resultado["messages"][-1].content) or "…"
                ok = True
                break
            except Exception:
                ok = False
                log.exception(
                    "Erro no grafo (membro %s, turno %s, tentativa %d)",
                    membro.id, turn_id, tentativa,
                )
                # Visto em produção (11/08): OpenRouter devolve 200 com choices=None
                # e o parse explode — o retry do cliente não cobre erro de PARSE.
                # Repetir o turno é seguro APENAS se nenhuma tool mutante rodou
                # (senão duplicaríamos lembrete/evento) — e quem sabe disso é o
                # próprio funil.
                efeito = await _turno_teve_efeito(turn_id)
                await emitir_de(cfg, "error", onde="grafo", tentativa=tentativa, efeito=efeito)
                if tentativa == 1 and not efeito:
                    continue
                if efeito:
                    texto = (
                        "Fiz o que você pediu, mas tropecei ao redigir a resposta. 😅 "
                        "Pode conferir com um \"que lembretes eu tenho?\" ou similar."
                    )
                else:
                    texto = "Ops, tropecei aqui do meu lado. 😅 Pode repetir, por favor?"
                break

    # O evento que sustenta quase todo o dashboard: uma linha por turno, com o
    # tempo que o usuário REALMENTE esperou — não o tempo de uma chamada de LLM.
    await emitir_de(
        cfg,
        "turn_completed",
        ok=ok,
        latencia_ms=round((time.perf_counter() - inicio) * 1000),
        espera_fila_ms=espera_fila_ms,
        tamanho_resposta=len(texto),
    )

    efeitos.limpar(turn_id)

    # Anexos que as tools registraram durante o turno (core/anexos.py):
    # o LLM só descreve; quem anexa é o sistema. Turno que falhou NÃO leva
    # anexo: "Ops, tropecei" acompanhado do RG da pessoa seria surreal —
    # coletamos mesmo assim para esvaziar o registro do turno.
    anexos = coletar(turn_id)
    if not ok:
        anexos = []
    return turn_id, [OutboundMessage(texto=texto, anexos=anexos)]
