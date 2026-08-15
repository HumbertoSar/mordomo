"""Emissor de eventos de produto (a "gestão à vista" começa aqui).

Cada evento é uma linha em product_events. Convenções de tipo (alinhadas ao
doc `gestao-a-vista-agente-whatsapp.md`):

  do turno de conversa:
    message_received · orchestrator_decision · orchestrator_parse_error ·
    tool_called · tool_result · llm_usage · turn_completed · message_sent ·
    reminder_created · feature_requested · feature_issue_created · error
  fora de turno (comandos, jobs — SEM turn_id por desenho):
    reminder_fired · proactive_sent · unknown_user · document_stored ·
    dashboard_sent · curation_run · invite_created · invite_used · invite_rejected

  ciclo de vida de uma necessidade (mesmo journey_id, possivelmente vários turnos):
    journey_started · journey_resolved · journey_abandoned · journey_reopened
  fatos do domínio de tarefas (também correlacionados por journey_id):
    task_created · task_completed · task_cancelled · task_reopened

Tipo novo emitido FORA de um turno tem que entrar em
`reporting.queries.SEM_TURNO_POR_DESENHO`, senão infla o KPI de eventos
órfãos do dashboard como falso positivo.

REGRA DO PROJETO: emita sempre por `emitir_de(config, ...)`, nunca por `emitir()`
direto, quando houver um `config` à mão. Evento de turno sem `session_id`/`turn_id` não
entra em nenhum funil — vira linha órfã que ninguém consegue cruzar depois.
(Foi exatamente o que aconteceu no primeiro dia de uso: tool_called,
reminder_created e message_sent nasceram sem sessão.)

Eventos operacionais comuns nunca derrubam a conversa (try/except deliberado).
Fatos que são invariantes do domínio, como tarefa + ciclo de vida da jornada,
usam ``evento_de`` na mesma transação: ou ambos persistem, ou ambos voltam."""

import logging

from .core import efeitos
from .db.models import ProductEvent
from .db.session import Sessao

log = logging.getLogger(__name__)


async def emitir(
    tipo: str,
    member_id: int | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    journey_id: str | None = None,
    **payload,
) -> None:
    # ANTES do try: o registro em memória de efeito mutante não pode se perder
    # junto com uma falha de banco — é ele que impede o retry de duplicar.
    efeitos.observar(tipo, turn_id, payload)
    try:
        async with Sessao() as s:
            s.add(
                ProductEvent(
                    tipo=tipo,
                    member_id=member_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    journey_id=journey_id,
                    payload=payload,
                )
            )
            await s.commit()
    except Exception:
        log.exception("Falha ao emitir evento de produto %s (conversa segue normal)", tipo)


def contexto_de(config) -> dict:
    """Extrai member/session/journey/turn do `configurable` do LangGraph.

    Mesmo canal por onde o member_id já viajava (ADR-003): a identidade, a
    jornada e o turno vêm SEMPRE do config, nunca do que o LLM disser."""
    bruto = config.get("configurable", {}) if isinstance(config, dict) else {}
    return {
        "member_id": bruto.get("member_id"),
        "session_id": bruto.get("session_id"),
        "turn_id": bruto.get("turn_id"),
        "journey_id": bruto.get("journey_id"),
    }


def evento_de(
    config, tipo: str, *, journey_id: str | None = None, **payload
) -> ProductEvent:
    """Constrói um evento para persistência na transação do domínio.

    Use somente quando o fato analítico é parte da mesma invariável do estado
    alterado (por exemplo, estado da tarefa + ciclo de vida da jornada).
    Eventos operacionais comuns continuam por :func:`emitir_de`, tolerante a falhas.
    """
    contexto = contexto_de(config)
    if journey_id is not None:
        contexto["journey_id"] = journey_id
    return ProductEvent(tipo=tipo, payload=payload, **contexto)


async def emitir_de(
    config, tipo: str, *, journey_id: str | None = None, **payload
) -> None:
    """`emitir` com contexto seguro e jornada opcionalmente explícita.

    Uma tool que CRIA a jornada ainda não a recebeu no ``configurable``; ela
    pode informar somente ``journey_id`` sem poder sobrescrever member/session/turn.
    """
    contexto = contexto_de(config)
    if journey_id is not None:
        contexto["journey_id"] = journey_id
    await emitir(tipo, **contexto, **payload)


def uso_de(mensagens) -> dict | None:
    """Soma os tokens das AIMessages que um nó produziu.

    Precisa existir porque os nós de subagente devolvem só a ÚLTIMA mensagem ao
    grafo pai: o consumo das chamadas intermediárias (o loop ReAct) sumiria do
    estado. Cada nó soma o que gastou e emite `llm_usage` — o que ainda dá, de
    graça, a quebra "custo de roteamento vs. custo de execução".

    Devolve None quando o provedor não informou uso (não emitimos evento vazio)."""
    entrada = saida = 0
    modelo = None
    for m in mensagens or []:
        uso = getattr(m, "usage_metadata", None)
        if not uso:
            continue
        entrada += uso.get("input_tokens") or 0
        saida += uso.get("output_tokens") or 0
        modelo = (getattr(m, "response_metadata", None) or {}).get("model_name") or modelo
    if not (entrada or saida):
        return None
    return {"modelo": modelo, "input_tokens": entrada, "output_tokens": saida}
