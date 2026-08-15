# 🤵 Mordomo da Família

Agente conversacional multi-agente (supervisor + subagentes) para uma família
brasileira — lembretes, agenda, tarefas acompanháveis e, em breve, e-mail e indicações de
filmes. Construído com **LangGraph** para aprender e demonstrar **analytics,
observabilidade (Langfuse) e evals** em um agente com usuários reais.

Dois canais no mesmo processo: **Telegram** (long polling, zero burocracia) e
**WhatsApp Cloud API oficial** (webhook assinado, dedupe de reentrega, janela de
24h vs. template). O núcleo não sabe em qual dos dois está — quem traduz é o
contrato de canal (`src/mordomo/channels/contract.py`, ADR-001), e por isso
migrar a família não custou tocar em nenhum agente. Ligar o WhatsApp é
preencher credenciais: [docs/whatsapp-fase3.md](docs/whatsapp-fase3.md).

## Stack

Python 3.11+ · LangGraph/LangChain 1.x · OpenRouter (um provedor, vários
modelos) · aiogram · Postgres (checkpointer + domínio + eventos de analytics) ·
Langfuse (traces) · APScheduler (proatividade) · dateparser (datas pt-BR).

## Subir em 10 minutos

Pré-requisitos: [uv](https://docs.astral.sh/uv/), Docker, um bot do Telegram
(@BotFather) e uma chave do [OpenRouter](https://openrouter.ai/keys).

```bash
make install                 # dependências (uv sync)
make up                      # Postgres (docker compose up -d)
cp .env.example .env         # preencha TELEGRAM_BOT_TOKEN e OPENROUTER_API_KEY
make db-init                 # tabelas
uv run python scripts/seed_familia.py --nome "Seu Nome" --telegram-id SEU_ID --papel adulto
make run                     # 🤵 a postos
```

**Windows não tem `make`.** Use o `tasks.ps1`, que espelha os mesmos alvos:

```powershell
.\tasks.ps1 install ; .\tasks.ps1 up ; .\tasks.ps1 db-init ; .\tasks.ps1 run
```

Duas pedras no caminho no Windows, ambas de primeira viagem:

- Se der *"a execução de scripts foi desabilitada neste sistema"*, libere scripts
  locais uma vez só: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
  (ou chame com `powershell -ExecutionPolicy Bypass -File .\tasks.ps1 <alvo>`).
- Se o projeto estiver dentro do OneDrive, **exclua `.venv/` da sincronização** —
  senão o `uv sync` falha com "Acesso negado" ao mexer em arquivo travado.

Seu telegram-id: mande /start ao bot e veja o log de `unknown_user`, ou use o
@userinfobot. Langfuse (opcional, recomendado): crie projeto grátis em
cloud.langfuse.com e preencha as chaves no `.env` — cada conversa vira um
trace navegável (user = membro, session = conversa do dia).

Teste no chat: `me lembra amanhã às 8h de pagar o boleto` · `marca dentista
sexta às 10h` · `cria uma tarefa para o Davi buscar os coletores` · `quais
tarefas estão abertas?` · `conclui a tarefa 3`.

## Desenvolvimento

```bash
make test    # testes rápidos (SQLite, sem rede/chaves/Docker)
make evals   # eval de datas pt-BR; --com-llm adiciona roteamento do supervisor
make lint
```

No Windows: `.\tasks.ps1 test`, `.\tasks.ps1 evals --com-llm`, `.\tasks.ps1 lint`.

O fluxo de aprendizado do projeto: operar com a família → ler traces no
Langfuse → medir (evals + eventos em `product_events`) → corrigir → re-medir.
Casos reais ruins viram linhas nos datasets de `evals/datasets/`.

A evolução coordenada de produto, Analytics, Observabilidade e Evaluation está
documentada em [docs/trilha-produto-aprendizado.md](docs/trilha-produto-aprendizado.md).
O próximo salto é medir a resolução de uma necessidade familiar ao longo de
vários turnos — não confundir uma resposta enviada com um problema resolvido.

## Estrutura

```
src/mordomo/
  channels/   contrato semântico + adapters (telegram, whatsapp) + webhook
  core/       grafo, estado, pipeline (nasce o trace), fábrica de LLM/agentes
  agents/     supervisor + subagentes lembretes, agenda, tarefas e cofre
  tools/      tools com analytics embutido + datas pt-BR (dateparser)
  db/         SQLAlchemy: domínio + eventos de produto
  scheduler.py / notify.py / identity.py / observability.py / analytics.py
evals/        datasets (datas pt-BR, roteamento) + runner
tests/        contrato de renderização (golden), datas, identidade
docs/adr/     as decisões de arquitetura que valem portfólio (9)
CLAUDE.md     guia para desenvolver com Claude Code
```

## Operar (VPS)

Deploy completo (Docker, migração dos dados, backup em cron, split dev/prod) em
[docs/deploy-vps.md](docs/deploy-vps.md). Resumo: `git clone`, `.env`,
`docker compose --profile bot up -d --build`.

O Telegram é long polling (nenhuma porta exposta). O WhatsApp precisa de HTTPS
público: um subdomínio entra como mais um site no Caddy que já serve a VPS,
com `reverse_proxy` para a porta interna do container — seção 8 do mesmo
documento.

## Decisões de arquitetura (resumo)

ADR-001 contrato de canal (migração Telegram→WhatsApp sem tocar no agente) ·
ADR-002 supervisor à mão (roteamento é eval de primeira classe) · ADR-003
thread = membro (memória sobrevive à troca de canal) · ADR-004 tool nativa vs.
MCP (decidir na fase 2) · ADR-005 privacidade e telemetria · ADR-006 latência e
provedores · ADR-007 janela de contexto · ADR-008 mordomo em grupo · ADR-009
WhatsApp direto na Cloud API, sem biblioteca intermediária. Detalhes em
`docs/adr/`.

> ⚠️ WhatsApp: apenas a **Cloud API oficial** (número de teste grátis para a
> família; depois chip dedicado). Nunca Evolution API/Baileys no número
> pessoal — risco real de banimento permanente.
