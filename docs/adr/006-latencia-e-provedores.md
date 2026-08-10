# ADR-006 — Cauda de latência do LLM: timeout e preferência de provedor

**Status:** aceito · 08/2026

## Contexto

Primeiro dia de uso real. Um turno de "me lembra amanhã às 8h da manhã de pagar
o boleto" levou **20,8 s** ponta a ponta. O trace do Langfuse deu o recibo por span:

```
LangGraph                20,82s
  supervisor             12,09s
    ChatOpenAI           10,57s   anthropic/claude-haiku-4.5
  lembretes               8,62s
    ChatOpenAI            5,49s   anthropic/claude-sonnet-4.5
    criar_lembrete        0,25s
    ChatOpenAI            2,65s   anthropic/claude-sonnet-4.5
```

O mesmo modelo, com o mesmo prompt, aparece nos outros traces do dia em 1,36s ·
1,59s · 1,60s · 6,40s · 10,57s. Não é volume de contexto nem o código: o
OpenRouter serve o mesmo modelo a partir de hosts diferentes e, sem preferência
declarada, a escolha é dele.

Investigação (32 chamadas controladas, `bench_provider`):

| configuração | p50 | p95 | max |
|---|---|---|---|
| como estava (sem preferência) | 1,32s | 2,41s | 2,41s |
| `provider.sort=latency` | 1,24s | 2,94s | 2,94s |
| `provider.sort=throughput` | 1,00s | 3,26s | 3,26s |
| `sort=latency` + timeout 8s | 0,98s | 1,79s | 1,79s |

**O pico de 10,6 s não se reproduziu em 32 chamadas.** A mediana sempre foi
saudável. O problema é de cauda, não de média — e uma cauda rara não se corrige
otimizando a mediana.

## Decisão

Tratar como problema de cauda, com a defesa mais barata possível:

- `timeout=8s` + `max_retries=2` em `chat_model()`. Uma chamada travada é
  abortada e refeita — quase sempre a segunda volta em ~1 s, então o pior caso
  percebido cai de "indeterminado" para ~9 s.
- `provider.sort=latency` como preferência declarada (configurável; `""`
  desliga). Ganho modesto e sem custo.
- **Não** trocar o modelo dos subagentes agora. Os 8,1 s de `sonnet-4.5` no nó
  de lembretes são o maior bloco isolado e `haiku` provavelmente daria conta de
  uma tarefa tão estreita ("passe a expressão à tool"), mas trocar sem o eval de
  qualidade de resposta (fase 2) seria trocar latência por regressão silenciosa.

### Atualização 10/08/2026 — experimento executado, decisão: manter sonnet

Com o eval de qualidade pronto (LLM-as-judge, juiz `gemini-2.5-flash` — família
diferente dos candidatos, de propósito), o experimento rodou pelo grafo REAL:

| agente | qualidade | p50 do turno |
|---|---|---|
| sonnet-4.5 | 12/12 (100%) | 5,1s |
| haiku-4.5 | 11/12 (92%) | 4,5s |

O haiku falhou em "reunião depois do almoço": perguntou o DIA em vez da HORA —
leve, sem invenção, mas é regressão de qualidade visível à família. E o ganho
de latência foi só 0,6s no p50: o turno é dominado por supervisor + rede, não
pelo modelo do subagente. Trocar compraria ~50% de economia num custo que é
~US$ 4/mês — não paga a regressão. Fica o sonnet; reavaliar se o dataset de
qualidade crescer com casos reais e o haiku empatar.

Nota de método: a primeira rodada do juiz reprovou o sonnet INJUSTAMENTE
("inventou" a data que o resolvedor de datas tinha resolvido de 'sexta') —
calibrar o juiz contra falsos positivos vem ANTES de confiar no veredito.

## Consequências

+ Pior caso percebido limitado, sem tocar em arquitetura.
+ A tabela acima é o "antes"; o dashboard da fase 1 dá o p50/p95 contínuo em uso
  real, que é o número que decide se ainda há problema.
− Um timeout agressivo pode abortar uma resposta legítima e lenta e gastar duas
  chamadas onde uma bastaria. Aceitável: o turno de conversa é curto.
− A decisão é **defensiva, não comprovada** — a cauda não foi reproduzida em
  laboratório. Se o p95 em produção continuar alto, reabrir com dado real.
