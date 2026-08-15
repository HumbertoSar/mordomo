# Trilha paralela — produto, Analytics, Observabilidade e Evaluation

**Status:** ativa · 08/2026

## Propósito

O Mordomo da Família é, antes de tudo, um laboratório com usuários reais para
aprender e demonstrar como operar um sistema agente. Melhorar o produto importa
porque utilidade gera uso; uso gera jornadas reais; jornadas reais geram dados
para Analytics, Observabilidade e Evaluation.

O objetivo não é maximizar funcionalidades. É criar ciclos curtos e verificáveis:

> produto útil → uso real → dados → diagnóstico → eval → melhoria → novo uso

A experiência será analisada por três cargas:

- **mental:** lembrar, decidir, acompanhar e cobrar;
- **logística:** levar e sincronizar informação entre pessoas, lugares e sistemas;
- **criativa:** pesquisar, resumir, traduzir e criar materiais.

## Princípios

1. **Instrumentar antes de expandir.** Feature sem hipótese e eventos não entra.
2. **Jornada acima de turno.** Turno mede uma pergunta/resposta; jornada mede a
   necessidade até seu desfecho.
3. **Resolução exige evidência.** Resposta do agente ou tool `ok=True` não prova
   que a necessidade familiar foi resolvida.
4. **Produto, trace e eval devem se encontrar.** IDs e taxonomia precisam permitir
   sair do KPI agregado, chegar ao trace e converter uma falha real em caso de eval.
5. **Privacidade por desenho.** Analytics guarda metadados e estados, nunca texto
   da conversa, valores do cofre, documentos, localização ou dados médicos.
6. **Amostra pequena, conclusão honesta.** Cinco usuários permitem estudo
   longitudinal e aprendizado operacional, não inferência estatística de mercado.

## Roadmap paralelo

| Etapa | Produto Mordomo | Analytics | Observabilidade | Evaluation | Evidência de saída |
|---|---|---|---|---|---|
| **1. Resolução** | Sem nova integração | Unidade `journey_id`, taxonomia e métricas de desfecho | Preparar correlação futura entre jornada e turno | Definir o que será avaliável | Eventos de jornada agregados sem confundir turno com resolução |
| **2. Tarefas** | Criar, atribuir, listar, concluir e reabrir tarefas | Funil pedido→aberta→concluída; tempo e retrabalho | Traces marcados com jornada/feature | Tool, argumentos e transições determinísticas | Uso real de tarefas com desfecho observável |
| **3. Compras** | Lista compartilhada; pendente, carrinho, comprado, não encontrado, online | Abandono e tempo por estado | Diagnóstico de falha por etapa | Evals multi-turno de atualização de lista | Jornada recorrente usada no WhatsApp |
| **4. Observabilidade de jornada** | Sem expansão deliberada | Segmentação por membro, jornada e carga | Do KPI ao trace: modelo, tool, latência, erro e estado | Casos reais ruins viram dataset | Falha agregada reproduzível em trace e eval |
| **5. Evals de jornada** | Melhorias guiadas pelos dados | Antes/depois das mudanças | Comparação de traces | Roteamento, tool/args, qualidade e multi-turno | Histórico versionado de regressão e melhoria |
| **6. Fim de semana** | Presença, agenda, alimentação e datas especiais | Adoção e resolução de coordenação familiar | Proatividade e dependências externas | Proatividade, contexto e coordenação multiusuário | Menos cobranças manuais e mais confirmações antecipadas |
| **7. Integrações** | Google Calendar e fontes externas escolhidas pelos dados | Valor incremental por integração | SLOs e falhas por dependência | Confiabilidade ponta a ponta | Integração só permanece se aumentar resolução |

Ficam deliberadamente fora do início: Uber/Maps, localização, compra de ingressos,
cartão/fraude e automações médicas. Têm custo, risco e dependências altos antes de
existir uma camada madura de jornada.

## Etapa 1 — contrato de Analytics por jornada

### Unidade de análise

Uma **jornada** é uma necessidade familiar que pode atravessar vários turnos,
dias, membros e ferramentas. Exemplo: “Davi realizar os exames” não termina
quando o Mordomo explica o jejum; termina quando há evidência do desfecho.

Relações:

```text
membro ─┬─ sessão (conversa do dia)
        └─ jornada ─┬─ turno 1 ─ eventos técnicos
                    ├─ turno 2 ─ eventos técnicos
                    └─ turno N ─ eventos técnicos
```

`journey_id` é correlação, não conteúdo. Eventos antigos continuam válidos com
`journey_id = null`.

### Eventos mínimos

| Evento | Campos obrigatórios de payload | Significado |
|---|---|---|
| `journey_started` | `journey_type`, `loads` | Necessidade passou a ser acompanhada |
| `journey_resolved` | — | Usuário/sistema forneceu evidência de conclusão |
| `journey_abandoned` | `reason` | Necessidade terminou sem resolução |
| `journey_reopened` | `reason` | Jornada antes encerrada voltou a ficar ativa |

Estados são derivados pela ordem dos fatos; não se grava “estado atual” dentro do
evento de Analytics. A futura entidade de domínio (tarefa/lista) terá seu próprio
estado operacional.

Taxonomia inicial de `journey_type`:

- `task`
- `shopping`
- `schedule`
- `reminder`
- `document`
- `research`
- `directions`
- `weekend_planning`
- `other`

`loads` aceita uma ou mais entre `mental`, `logistics` e `creative`.

### Métrica principal

**Demandas resolvidas por membro ativo por semana**

Ela só será publicada quando houver jornadas reais emitidas. Até lá, o dashboard
deve mostrar ausência de base, não zero como se fosse desempenho ruim.

### Métricas de apoio

- jornadas iniciadas;
- jornadas resolvidas;
- jornadas abandonadas;
- jornadas abertas ao final da janela;
- taxa de resolução entre jornadas com desfecho;
- tempo mediano e p95 até resolução;
- reaberturas;
- distribuição por tipo de jornada;
- distribuição por carga.

A taxa de resolução usa como denominador apenas jornadas com desfecho:

```text
resolvidas / (resolvidas + abandonadas)
```

Jornadas abertas aparecem separadamente. Misturá-las no denominador penalizaria
jornadas recentes e tornaria períodos incomparáveis.

### Limites da Etapa 1

- Não inferir resolução pelo texto com LLM em produção.
- Não considerar `turn_completed`, `message_sent` ou `tool_result(ok=True)` como
  resolução.
- Não retroclassificar automaticamente todo o histórico.
- Não criar dashboard vistoso antes de haver eventos reais suficientes.
- Não alegar redução de carga apenas com telemetria comportamental; complementar
  depois com confirmações curtas e entrevistas com a família.

## Portões de avanço

A Etapa 2 começa quando:

- o contrato de eventos e a correlação por `journey_id` estiverem testados;
- agregações tratarem resolução, abandono, reabertura e jornadas abertas;
- eventos sem jornada continuarem válidos;
- migração, suíte e lint estiverem verdes;
- estiver claro como o Subagente de Tarefas produzirá evidência de conclusão.

A Etapa 3 só começa após uso real suficiente da Etapa 2 para revelar pelo menos
um atrito concreto, uma falha convertível em eval e uma hipótese de melhoria.
