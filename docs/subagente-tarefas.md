# Subagente de Tarefas — primeira fonte real de jornadas

**Status:** em implementação · 08/2026

## Hipótese

Se a família puder registrar e encerrar pendências pelo WhatsApp, o Mordomo
reduzirá carga mental e logística e passará a produzir evidência real de
resolução, não apenas de turnos respondidos.

## Fatia mínima

O subagente permite:

1. criar tarefa;
2. atribuir a um membro da família ou deixar sem responsável;
3. escolher escopo privado ou compartilhado;
4. informar prazo opcional;
5. listar abertas e, sob pedido, encerradas;
6. concluir;
7. cancelar;
8. reabrir.

Não entram ainda: subtarefas, comentários, anexos, recorrência, cobrança
proativa, dependências, lista de compras especializada ou sincronização externa.

## Modelo de domínio

`Task` é o estado operacional que o produto precisa consultar:

- `id`;
- `titulo`;
- `criado_por`;
- `responsavel_id` opcional;
- `compartilhada`;
- `prazo_utc` opcional;
- `status`: `aberta`, `concluida` ou `cancelada`;
- `journey_id` único;
- datas de criação e atualização.

Analytics continua append-only em `product_events`; não usa a tabela de tarefas
como substituto dos fatos históricos.

## Visibilidade e segurança

- tarefa privada: visível apenas para quem criou;
- tarefa compartilhada: visível para a família;
- atribuir a outra pessoa torna a tarefa compartilhada;
- responsável é resolvido deterministicamente pelo cadastro, nunca inventado
  pelo LLM;
- título e nomes não entram no payload de Analytics;
- qualquer membro que enxergue uma tarefa compartilhada pode atualizá-la nesta
  primeira versão; permissões mais finas só serão adicionadas se o uso real
  mostrar necessidade.
- transições são condicionais e atômicas no banco: duas conclusões simultâneas
  não podem emitir duas resoluções para a mesma jornada.
- estado da tarefa e fatos essenciais da jornada compartilham uma transação;
  uma falha não pode persistir apenas um dos lados.

O padrão do agente será **privado** quando o usuário não indicar família ou outra
pessoa. É a escolha conservadora para não expor uma tarefa sensível por engano.

## Ligação com jornadas

| Ação de domínio | Evento de jornada | Estado analítico final |
|---|---|---|
| criar | `journey_started` | aberta |
| concluir | `journey_resolved` | resolvida |
| cancelar | `journey_abandoned` (`user_cancelled`) | abandonada |
| reabrir | `journey_reopened` | aberta |

`journey_type = task`.

Cargas determinadas sem LLM:

- toda tarefa: `mental`;
- tarefa compartilhada ou atribuída a outra pessoa: também `logistics`.

Não usamos `creative` automaticamente: isso descreve o trabalho executado, não
o simples ato de acompanhar uma pendência.

## Eventos de produto

Além dos eventos de jornada e dos eventos genéricos de tool, serão emitidos:

- `task_created`;
- `task_completed`;
- `task_cancelled`;
- `task_reopened`.

Payloads levam IDs, estado, escopo e presença de prazo; nunca o título.

## Evidência de sucesso inicial

A feature estará tecnicamente pronta quando:

- transições válidas e inválidas estiverem testadas;
- isolamento privado e visibilidade compartilhada estiverem testados;
- eventos compartilharem o mesmo `journey_id`;
- retries não puderem duplicar mutações;
- concorrência entre membros não puder duplicar resolução;
- migração subir, descer e subir novamente;
- testes, lint, evals e CI passarem.

Sucesso de produto só poderá ser avaliado depois de uso real. A primeira leitura
será: tarefas criadas, abertas, resolvidas, abandonadas, reabertas e tempo até
resolução — segmentadas por membro e carga, sem alegar impacto estatístico.
