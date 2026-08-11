# ADR-005 — Privacidade e telemetria (traces completos, segredos mascarados)

**Status:** aceito · 08/2026 (decisão de 09/08, escrita quando o Cofre a tornou urgente)

## Contexto

O projeto usa Langfuse Cloud: cada conversa vira um trace navegável, e as
frases reais da família são a matéria-prima dos evals — o motivo nº 1 do
projeto existir. Ao mesmo tempo, o repositório é público, um dos membros era
menor até a fase 1, e o Cofre passa a guardar dados sensíveis DE VERDADE
(números de documento, CEP, carteirinha do plano).

## Decisão

1. **Conteúdo conversacional completo nos traces.** Frases do dia a dia
   ("me lembra amanhã…") vão inteiras ao Langfuse Cloud — sem elas não há
   curadoria de eval nem depuração real. A família consentiu, sabendo que o
   conteúdo das conversas fica em um serviço de terceiros (região US).
2. **Valores do Cofre e conteúdo de documentos NUNCA vão ao trace.** O SDK do
   Langfuse aceita `mask`: toda string que as tools do Cofre leem ou gravam é
   registrada em `privacidade.py` e substituída por `«cofre»` em qualquer parte
   de qualquer trace. Imagens/arquivos nunca saem do Postgres da VPS.
3. **Dashboard só agrega.** `docs/dashboard.html` (público, no repo) lê
   exclusivamente agregações de `product_events` — nenhum conteúdo de conversa,
   nenhum payload do Cofre.
4. **Dados em repouso ficam na VPS da família** (Postgres em container, sem
   porta pública), com dump diário local. Não há criptografia por coluna no
   MVP — o modelo de ameaça é vazamento acidental via telemetria/repo, não
   invasor com root na VPS (quem tem root lê o banco de qualquer forma).
5. **Retenção:** traces seguem a retenção do plano do Langfuse; o banco local
   guarda tudo até decisão em contrário da família. Membro que sair pode pedir
   remoção (delete por member_id em todas as tabelas + pedido de purge no
   Langfuse).

## Limites conhecidos (dito com todas as letras)

- O segredo aparece no CHAT (Telegram) — é o canal que a família escolheu; a
  proteção daquele conteúdo é do Telegram, não nossa.
- O mascaramento cobre o que passa pelas tools do Cofre. Se alguém ditar o CPF
  numa frase solta sem guardar no Cofre, essa frase vai ao trace como qualquer
  conversa (item 1). Mitigação: o mordomo incentiva "guarde no cofre".
- `product_events` não recebe valores do Cofre por construção (payloads levam
  chaves e ids, nunca valores) — coberto por teste.

## Consequências

+ Evals continuam alimentados por conversa real; o que é segredo de verdade
  tem barreira técnica, não só promessa.
− Mais uma peça para manter: toda tool nova do Cofre TEM que registrar valores
  em `privacidade.registrar_segredo` (regra no CLAUDE.md + teste).
