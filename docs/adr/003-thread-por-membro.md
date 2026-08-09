# ADR-003 — Thread do checkpointer = membro (não chat do canal)

**Status:** aceito · 08/2026

## Contexto
LangGraph persiste estado por `thread_id`. Opções: thread por chat do canal
(telegram chat_id / wa_id) ou por pessoa.

## Decisão
`thread_id = "membro-{member_id}"`. Identidades de canal ficam em
`channel_identities` (telegram user_id, wa_id) e são resolvidas na borda.
`member_id` viaja no `configurable` e as tools SEMPRE o leem de lá (nunca
confiam no LLM para dizer quem é o usuário).

## Consequências
+ Migrar Telegram → WhatsApp preserva histórico, memória e preferências.
+ Permissões por papel (adulto/criança) resolvidas na borda, não no prompt.
− Contexto compartilhado da família (agenda) não mora na thread — vive no
  banco/Store, acessado por tools (decisão deliberada: privado na thread,
  comum no banco).
