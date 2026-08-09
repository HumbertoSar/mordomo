# ADR-001 — Contrato interno de mensagens + adapters por canal

**Status:** aceito · 08/2026

## Contexto
Desenvolvemos no Telegram (grátis, long polling, sem burocracia) mas o destino
é o WhatsApp (família brasileira). Os canais divergem em botões (∞ vs. 3
reply buttons/10 itens de lista), formatação, proatividade (livre vs. janela
de 24h + templates) e webhook (at-least-once com retries por 7 dias no WhatsApp).

## Decisão
O núcleo (LangGraph) consome `InboundMessage` e produz `OutboundMessage`
**semântica** (texto + intenção de interação), nunca widgets. Renderers por
canal degradam via `plan_rendering()` (função pura, golden tests). Proatividade
via `notify()` abstraído. Texto-primeiro; botões são progressive enhancement.
Referências de quem já fez: BuilderBot (providers), Rasa (channel connectors),
Bot Framework (Activity schema).

## Consequências
+ Migração de canal não toca o agente; contract tests garantem equivalência.
+ Replay de conversas do Telegram re-renderizadas para WhatsApp (dry-run).
− Um nível a mais de indireção; interações novas exigem regra de degradação.
