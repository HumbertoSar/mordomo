# ADR-002 — Supervisor construído à mão (não langgraph-supervisor)

**Status:** aceito · 08/2026

## Contexto
O objetivo do projeto é APRENDER (analytics/observabilidade/evals) espelhando
a arquitetura orquestrador + subagentes. Existe pacote pronto
(`langgraph-supervisor`), mas o roteamento é justamente o que queremos medir
e melhorar.

## Decisão
Supervisor manual: structured output (`Decisao`) + `Command(goto=...)`.
Subagentes como nós ReAct (`criar_agente`, stateless; histórico no grafo pai).
A acurácia do roteamento é eval de primeira classe (`evals/datasets/roteamento.json`).

## Consequências
+ Controle total do prompt de roteamento; evolução medida por eval.
+ Instrumentação explícita (`orchestrator_decision` em cada decisão).
− Mais código nosso. Se virar fardo, migrar para o pacote e comparar no eval
  (experimento interessante por si só).
