# Mordomo da Família — guia para o Claude Code

Agente conversacional multi-agente (LangGraph) para a família do Humberto,
construído para APRENDER **analytics, observabilidade e evals** — e servir de
portfólio. Canal atual: Telegram (long polling). Destino: WhatsApp Cloud API
oficial (fase 3). O plano completo vive no projeto Claude "Analytics para
Agentes Conversacionais" (docs `mordomo-familia-arquitetura-e-possibilidades-v2.md`,
`avaliacao-ideia-e-propostas-aprendizado.md`, `gestao-a-vista-agente-whatsapp.md`).

## Comandos

```bash
make install   # uv sync
make up        # Postgres via docker compose
make db-init   # alembic upgrade head (NÃO é mais create_all)
make seed      # cadastra família de exemplo (ou scripts/seed_familia.py --nome ... --telegram-id ...)
make run       # inicia o bot (precisa de TELEGRAM_BOT_TOKEN e OPENROUTER_API_KEY no .env)
make test      # pytest — SEM rede/chaves/Docker (SQLite via tests/conftest.py)
make evals     # eval de datas pt-BR; `uv run python evals/run_evals.py --com-llm` inclui roteamento
uv run python evals/experimentos_langfuse.py    # espelha datasets no Langfuse e registra Experiments
make lint      # ruff

uv run python -m mordomo.reporting.dashboard --dias 30   # gera docs/dashboard.html
uv run python scripts/preview_dashboard.py               # dashboard com dados SINTÉTICOS (mexeu no dashboard? veja aqui, sem deploy)
uv run alembic revision --autogenerate -m "..."          # nova migração
powershell -ExecutionPolicy Bypass -File scripts/backup.ps1
```

**No Windows não existe `make`**: use `.\tasks.ps1 <alvo>` (mesmos nomes). Se o
PowerShell recusar, `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` uma vez.

## Arquitetura (mapa mental)

```
Telegram (aiogram) ─┐                       ┌─ agents/supervisor.py  (roteia: Command/goto)
WhatsApp (fase 3) ──┤→ channels/contract.py │→ agents/lembretes.py → tools/lembretes.py → scheduler.py
                    │  (Inbound/Outbound    │→ agents/agenda.py    → tools/agenda.py
                    │   SEMÂNTICOS)         │→ agents/cofre.py     → tools/cofre.py
                    │                       └─ core/graph.py (StateGraph + checkpointer Postgres)
                    └→ core/pipeline.py — nasce o trace (Langfuse), o turn_id, o LOCK por
                       thread e a trava de retry (core/efeitos.py)
notify.py: proatividade abstraída (scheduler → canal)  ·  identity.py: (canal, id) → member
agents/_base.py: fábrica NoSubagente — subagente novo = prompt + 1 linha
```

## Regras do projeto (importantes — não violar)

1. **O núcleo nunca conhece o canal** (ADR-001). Nada de `if canal == "telegram"`
   fora de `channels/`. Novas interações visuais entram como semântica no
   contrato + regra de degradação em `plan_rendering()` + golden test.
2. **Thread = membro** (ADR-003). `member_id` vem SEMPRE de
   `config["configurable"]` — nunca do texto do LLM.
3. **Datas pt-BR só via `tools/datas.resolver_data`** (híbrido: LLM extrai a
   expressão, parser determinístico resolve). Se devolver None, o agente
   PERGUNTA — nunca inventa data.
4. **Toda tool nova emite analytics por `analytics.emitir_de(config, ...)`** —
   nunca `emitir()` cru quando houver `config` à mão. `emitir_de` puxa
   member/session/**turn_id** do `configurable`; evento sem `turn_id` não entra
   em nenhum funil e vira linha órfã (o dashboard tem um KPI só para vigiar
   isso, e ele tem que ficar em ZERO). Evento emitido FORA de um turno
   (comando, job proativo) tem que entrar em
   `reporting/queries.py::SEM_TURNO_POR_DESENHO`, senão vira falso órfão nesse
   KPI. Todo caminho de saída emite `tool_result`,
   inclusive os de falha — o `motivo` é o que vira caso novo no eval.
   Falha de analytics nunca derruba a conversa.
5. **Mexeu em prompt ou em `resolver_data` → rode `make evals`** e anote o
   antes/depois (é o material do portfólio). Frases reais boas/ruins dos
   traces do Langfuse viram casos novos nos datasets.
6. **Respostas em tom de mordomo, curtas, texto-primeiro** (formato WhatsApp:
   sem markdown pesado, sem tabelas).
7. **Testes (`make test`) não podem depender de rede, chaves ou Docker.**
   Fixtures de membro/config: use `tests/apoio.py` (criar_membro/cfg_de) —
   não copie `_membro` local. Ids/nomes únicos por execução quando o teste
   grava no banco compartilhado da sessão.
8. **NUNCA** adicionar Evolution API/Baileys/WPPConnect (risco real de ban do
   número). WhatsApp = Cloud API oficial via pywa (fase 3).
9. Não commitar `.env`. Segredos só por variável de ambiente.
10. **Subagente novo = `NoSubagente(nome, tools, prompt)`** (agents/_base.py) —
    não copie o padrão de nó na mão. Testes injetam fake pelo atributo
    `agente` da instância (ver tests/test_grafo.py).
11. **Tool que GRAVA algo entra em `core/efeitos.py::TOOLS_MUTANTES`** — é a
    trava que impede o retry do pipeline de executá-la duas vezes. Tool que
    resolve data usa `tools/_comum.resolver_ou_instruir` (o texto de falha é
    prompt compartilhado entre os agentes) e `fmt_data`.
12. **Convenção do funil: busca que não achou = `ok=False` + `motivo`**
    ("nao_encontrado", "data_nao_entendida"…) — é o que alimenta o ranking de
    falhas e a curadoria. `tool_called` sempre na ENTRADA da tool.
13. **Grupo (ADR-008)**: cofre e documentos respondem no grupo da família,
    mas só com o COMPARTILHADO — item/documento "só pra mim" é exclusivo do
    privado (filtro determinístico via `grupo_id` do configurable, nunca
    prompt). `/convidar` e `/vincular` só no privado (o código é segredo).
14. **Privacidade (ADR-005)**: payload de analytics leva chave/id, nunca
    valor nem texto de conversa; issue no repo público leva só o título
    (detalhe atrás de GITHUB_ISSUES_DETALHADAS, para repo privado); valor do
    cofre passa por `privacidade.registrar_segredo` antes de voltar ao LLM.

## Gotchas conhecidos

- `core/llm.py::criar_agente` tenta `langchain.agents.create_agent` (LangChain
  1.x) e cai para `langgraph.prebuilt.create_react_agent`. Se ambos falharem,
  a API mudou — confira a documentação antes de "consertar" na força.
- OpenRouter: use modelos com bom tool calling (o supervisor usa structured
  output). Modelos "free" fracos falham silenciosamente no roteamento.
- Langfuse sem chaves = desligado de propósito (o bot roda igual). Custos de
  modelos OpenRouter podem precisar de cadastro manual no Langfuse (Settings → Models).
- Checkpointer Postgres entra por context manager em `main.py` (AsyncExitStack);
  SQLite/testes caem em InMemorySaver.
- **Windows**: `plataforma.preparar()` é obrigatório ANTES de `asyncio.run()` em
  todo ponto de entrada. O psycopg async se recusa a rodar no ProactorEventLoop
  (padrão do Windows) e o console cp1252 estoura em `✓`, `──` e emoji.
- **Alembic + checkpointer**: as tabelas do LangGraph (`checkpoint*`) vivem no
  mesmo banco e NÃO estão em `Base.metadata`. Sem o `include_object` de
  `migrations/env.py`, o autogenerate escreve `drop_table` para elas e apaga o
  histórico de conversas. Confira toda migração nova antes de aplicar.
- **Alembic + logging**: `fileConfig()` reconfigura o logging do processo inteiro
  e o `alembic.ini` põe o root em WARNING. Ao rodar migração programaticamente,
  `cfg.attributes["configure_logger"] = False` — senão o bot sobe mudo.
- Latência: o OpenRouter serve o mesmo modelo de hosts diferentes e a cauda
  chega a 10s (ADR-006). `chat_model()` já tem timeout + retry.
- **PowerShell 5.1 + .ps1 sem BOM**: o parser lê o arquivo como ANSI — um
  travessão (—) dentro de STRING vira aspa curva mojibakada e quebra a sintaxe
  do script inteiro. Strings dos .ps1 ficam sem acento/travessão de propósito.
  E `Set-Content`/pipeline do PS corrompe UTF-8 dos fontes: edite .py com as
  ferramentas de edição, nunca com `-replace` + `Set-Content`.
- `.pytest_mordomo.db` às vezes sobrevive entre execuções (OneDrive segura o
  arquivo no Windows) — por isso os testes usam nomes/ids únicos por execução.

## Feito (fase 1)

Instrumentação fechada: `turn_id` em todo evento, `turn_completed` com latência,
`llm_usage` por nó (custo de rotear vs. executar), `orchestrator_parse_error`.
Leitura em `reporting/` (queries + `docs/dashboard.html` autocontido). Alembic
com baseline. Backup em `scripts/backup.ps1`. ADR-006 sobre latência. ADR-007:
janela de contexto (o histórico fica no checkpointer; ao LLM vão as últimas N).
Evals com histórico (`--salvar` → `evals/results/history.csv`, delta automático).
Grafo coberto sem rede (`tests/test_grafo.py`, fakes do structured output).
Áudio: Groq/Whisper no adapter (`channels/transcricao.py`; sem GROQ_API_KEY =
recusa simpática).

**Revisão 08/2026** (segurança/robustez/qualidade, 20+ commits): issues
públicas sem conversa da família; grupo compartilha só o compartilhado; flush
protegido + fatiamento no max_texto do canal; notificar() devolve False;
senha do Postgres via env; lock por thread; /vincular atômico; retry com
memória de efeitos (core/efeitos.py) e anexos deduplicados; TZDateTime
(naive→aware); máscara do Langfuse reposta no boot; cofre sem oráculo de
existência nem curinga de LIKE; backups .sql.gpg opcionais
(BACKUP_PASSPHRASE); fábrica NoSubagente; reporting sem queries mortas;
tools/lembretes testada (`tests/test_lembretes_tools.py`).

## Roadmap (o que falta — fase 2+)

- [ ] Memória de longo prazo (LangGraph Store + extração em background / LangMem)
- [ ] Subagente Tarefas (listas por pessoa + compartilhadas)
- [ ] Subagente Curador (TMDB + onde assistir no BR, perfil por membro)
- [ ] Subagente Mensageiro (Gmail) com `interrupt()` — HITL de verdade
- [ ] Recorrência de lembretes ("todo dia 5") + briefing matinal (job proativo)
- [x] `/vincular` (onboarding sem seed script) — /convidar gera código (só
      adulto), /vincular consome; quem convida decide o papel do convidado
- [ ] Google Calendar no lugar da tabela própria — decidir ADR-004 (nativa vs. MCP)
- [x] Datasets → Langfuse Datasets/Experiments (`evals/experimentos_langfuse.py`:
      datas+roteamento; qualidade ainda só local) + Evaluator LLM-as-judge
      "qualidade-mordomo" ativo em produção (juiz gemini, turno raiz, score 0-1)
- [ ] Simulador de personas (OpenEvals)
- [ ] Fase 3 WhatsApp: pywa + FastAPI, checklist da seção 4.4 do doc v2
