# ADR-011 — Contrato de medição: turno, resultado e jornada

**Status:** aceito  
**Data:** 2026-08-18

## Contexto

O Mordomo já media execução técnica (`tool_called`, `tool_result`, latência, custo e envio), mas o produto cresceu para capacidades persistentes e fluxos multi-turno. Um turno concluído não comprova que a necessidade da família foi resolvida.

A Agenda tornou o problema visível: preparar em um turno, confirmar em outro e criar no Google ou na agenda nativa é uma única necessidade, enquanto cada tool continua sendo uma operação técnica separada.

## Decisão

Separar três níveis:

1. **Turno concluído:** o processamento técnico terminou.
2. **Resultado comprovado:** há evidência determinística de valor ou efeito da capacidade.
3. **Jornada resolvida:** uma necessidade durável ou multi-turno chegou a um desfecho comprovado.

Todo novo `ProductEvent` recebe automaticamente:

- `release`: versão do processo, quando descoberta;
- `event_schema`: versão do contrato analítico.

Eventos antigos sem esses campos continuam válidos e aparecem como release desconhecida e schema legado.

Uma taxonomia central projeta eventos existentes em `capability`, `operation`, `kind`, `dependency`, `evidence` e regra de prova. Ela não transporta texto, título, e-mail, local, link ou token.

### Provas de resultado

- Leitura: `tool_result(ok=True)` e `message_sent` no mesmo `turn_id`.
- Persistência local: sucesso emitido somente após commit.
- Efeito externo: sucesso com destino comprovado no payload.
- A unidade conservadora de valor é `(turn_id, capability, operation)`: retries da
  mesma operação no turno continuam visíveis como execuções, mas não duplicam o
  resultado comprovado.
- Preparação, descarte técnico, turno concluído e mensagem enviada isoladamente não contam como resultado.

### Jornada `calendar_create`

- nasce junto com a proposta persistida;
- o mesmo `journey_id` atravessa preparação e confirmação;
- resolve somente após efeito Google confirmado ou persistência nativa concluída;
- criação nativa, conclusão da proposta e resolução da jornada compartilham a
  mesma transação;
- no Google, uma lease curta permite retomar reivindicação órfã com a mesma chave
  determinística; worker antigo não pode liberar a lease retomada;
- falha externa devolve a proposta e mantém a jornada aberta;
- descarte efetivo abandona exatamente as jornadas das propostas removidas;
- repetição idempotente não emite nova resolução.

## Dashboard

O painel passa a ter cinco níveis de leitura:

1. Visão executiva;
2. Produto e jornadas;
3. Operação;
4. Observabilidade;
5. Evaluation.

Rótulos distinguem sucesso técnico de valor e mostram ausência de base em vez de inventar zero ou porcentagem.

## Consequências

- O histórico permanece consultável sem migração.
- As consultas e transições críticas rodam no CI em SQLite e PostgreSQL efêmero.
- Tools novas precisam entrar na taxonomia; um teste de cobertura falha se forem esquecidas.
- Jornadas não são retrocriadas nem atribuídas artificialmente a toda conversa.
- O contrato melhora analytics local e correlação, mas não substitui traces Langfuse nem evaluation.

## Fora desta decisão

- datasets e experiments Langfuse;
- novos judges e gates de eval;
- mascaramento adicional de traces remotos;
- atualização da página pública do portfólio.

Esses itens pertencem às fatias Q2 e Q3.
