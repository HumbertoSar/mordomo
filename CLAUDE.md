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
make db-init   # cria tabelas
make seed      # cadastra família de exemplo (ou scripts/seed_familia.py --nome ... --telegram-id ...)
make run       # inicia o bot (precisa de TELEGRAM_BOT_TOKEN e OPENROUTER_API_KEY no .env)
make test      # pytest — SEM rede/chaves/Docker (SQLite via tests/conftest.py)
make evals     # eval de datas pt-BR; `uv run python evals/run_evals.py --com-llm` inclui roteamento
make lint      # ruff
```

## Arquitetura (mapa mental)

```
Telegram (aiogram) ─┐                       ┌─ agents/supervisor.py  (roteia: Command/goto)
WhatsApp (fase 3) ──┤→ channels/contract.py │→ agents/lembretes.py → tools/lembretes.py → scheduler.py
                    │  (Inbound/Outbound    │→ agents/agenda.py    → tools/agenda.py
                    │   SEMÂNTICOS)         └─ core/graph.py (StateGraph + checkpointer Postgres)
                    └→ core/pipeline.py — nasce o trace (Langfuse) e os eventos (analytics.py)
notify.py: proatividade abstraída (scheduler → canal)  ·  identity.py: (canal, id) → member
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
4. **Toda tool nova emite analytics** (`tool_called`/`tool_result` via
   `analytics.emitir`) e falha de analytics nunca derruba a conversa.
5. **Mexeu em prompt ou em `resolver_data` → rode `make evals`** e anote o
   antes/depois (é o material do portfólio). Frases reais boas/ruins dos
   traces do Langfuse viram casos novos nos datasets.
6. **Respostas em tom de mordomo, curtas, texto-primeiro** (formato WhatsApp:
   sem markdown pesado, sem tabelas).
7. **Testes (`make test`) não podem depender de rede, chaves ou Docker.**
8. **NUNCA** adicionar Evolution API/Baileys/WPPConnect (risco real de ban do
   número). WhatsApp = Cloud API oficial via pywa (fase 3).
9. Não commitar `.env`. Segredos só por variável de ambiente.

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

## Roadmap (o que falta — fase 2+)

- [ ] Memória de longo prazo (LangGraph Store + extração em background / LangMem)
- [ ] Subagente Tarefas (listas por pessoa + compartilhadas)
- [ ] Subagente Curador (TMDB + onde assistir no BR, perfil por membro)
- [ ] Subagente Mensageiro (Gmail) com `interrupt()` — HITL de verdade
- [ ] Áudio: transcrição Whisper via Groq no adapter (WhatsApp brasileiro = áudio)
- [ ] Recorrência de lembretes ("todo dia 5") + briefing matinal (job proativo)
- [ ] `/vincular` (onboarding sem seed script) + permissões por papel de fato
- [ ] Google Calendar no lugar da tabela própria — decidir ADR-004 (nativa vs. MCP)
- [ ] Datasets → Langfuse Datasets/Experiments; simulador de personas (OpenEvals)
- [ ] Fase 3 WhatsApp: pywa + FastAPI, checklist da seção 4.4 do doc v2
