# ADR-007 — Janela de contexto por chamada (o histórico fica, o prompt não)

**Status:** aceito · 08/2026

## Contexto

O primeiro dashboard com dado real (8 turnos, 10/08/2026) revelou um problema
que nenhum teste ou eval tinha visto: os tokens de ENTRADA do supervisor
crescendo turno a turno, monotonicamente —

```
929 → 1005 → 1091 → 1176 → 1284 → 1349 → 1400 → 1501   (~72 tokens/turno)
```

É consequência direta do ADR-003 (thread = membro): `state["messages"]` acumula
para sempre, e cada chamada recebia a conversa INTEIRA — o roteador (que só
escolhe entre três destinos) e o subagente (a preço de sonnet, responsável por
78% do custo do dia). Sem teto: uma família ativa por um mês levaria o prompt
do roteador a dezenas de milhares de tokens, pagando mais e esperando mais a
cada dia de uso.

## Decisão

`core/contexto.py::janela()`: ao LLM vão só as últimas N mensagens
(`CONTEXTO_JANELA_MENSAGENS`, padrão 8; 0 desliga). Aplicada no supervisor e
nos dois subagentes.

Regras do corte:
- Sempre começa em `HumanMessage` — cortar entre um tool_call e sua
  `ToolMessage` quebra a API do provedor (coberto por teste).
- Corta na CHAMADA, não na persistência: o checkpointer continua guardando a
  conversa completa. O ADR-003 fica intacto — a memória pertence à pessoa; o
  que muda é quanto dela viaja em cada prompt.

## Prova (antes de confiar)

1. **Acurácia**: 3 casos multi-turno novos no eval de roteamento — follow-ups
   que SÓ funcionam com contexto ("cancela o segundo", "muda pra sábado de
   manhã", "e no domingo?"). O eval passa pela mesma `janela()` de produção:
   **15/16 (94%)** com a janela ativa, os três multi-turno ✓. O único ✗ é o
   caso de produto pré-existente ("não esquece do dentista…"), não relacionado.
2. **Teto**: conversa controlada de 10 turnos pelo pipeline real —
   `623 → 659 → 702 → 732 → 745 → 741 → 735 → 735 → 720 → 724`.
   Cresce até a janela encher (turno 4) e ESTABILIZA em ~730. Sem a janela, a
   série real de produção seguia subindo sem limite.

## Consequências

+ Custo e latência por turno ficam constantes com a idade da conversa.
+ O subagente (78% do custo) também para de crescer.
− O supervisor perde memória além das últimas 8 mensagens. Para ROTEAR, o
  recente basta — os casos multi-turno provam. Referências antigas ("aquilo que
  te falei semana passada") são problema de MEMÓRIA DE LONGO PRAZO, que o
  roadmap resolve com LangGraph Store/LangMem na fase 2 — solução certa para
  esse problema, em vez de prompt infinito.
− N=8 é chute calibrado com pouco dado. O `llm_usage` por nó no dashboard diz
  se está apertado (follow-ups falhando → subir) ou folgado demais.
