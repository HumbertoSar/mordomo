"""Emissor de eventos de produto (a "gestão à vista" começa aqui).

Cada evento é uma linha em product_events. Convenções de tipo (alinhadas ao
doc `gestao-a-vista-agente-whatsapp.md`):

  message_received · orchestrator_decision · orchestrator_parse_error ·
  tool_called · tool_result · llm_usage · turn_completed · message_sent ·
  reminder_created · reminder_fired · proactive_sent · unknown_user · error

REGRA DO PROJETO: emita sempre por `emitir_de(config, ...)`, nunca por `emitir()`
direto, quando houver um `config` à mão. Evento sem `session_id`/`turn_id` não
entra em nenhum funil — vira linha órfã que ninguém consegue cruzar depois.
(Foi exatamente o que aconteceu no primeiro dia de uso: tool_called,
reminder_created e message_sent nasceram sem sessão.)

Falha de analytics NUNCA derruba a conversa (try/except deliberado)."""

import logging

from .db.models import ProductEvent
from .db.session import Sessao

log = logging.getLogger(__name__)


async def emitir(
    tipo: str,
    member_id: int | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    **payload,
) -> None:
    try:
        async with Sessao() as s:
            s.add(
                ProductEvent(
                    tipo=tipo,
                    member_id=member_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    payload=payload,
                )
            )
            await s.commit()
    except Exception:
        log.exception("Falha ao emitir evento de produto %s (conversa segue normal)", tipo)


def contexto_de(config) -> dict:
    """Extrai member/session/turn do `configurable` do LangGraph.

    Mesmo canal por onde o member_id já viajava (ADR-003): a identidade e o
    turno vêm SEMPRE do config, nunca do que o LLM disser."""
    bruto = config.get("configurable", {}) if isinstance(config, dict) else {}
    return {
        "member_id": bruto.get("member_id"),
        "session_id": bruto.get("session_id"),
        "turn_id": bruto.get("turn_id"),
    }


async def emitir_de(config, tipo: str, **payload) -> None:
    """`emitir` já com member/session/turn preenchidos a partir do config."""
    await emitir(tipo, **contexto_de(config), **payload)


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
