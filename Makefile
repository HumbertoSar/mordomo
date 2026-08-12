.PHONY: install up down db-init seed run test evals lint replay

install:        ## instala dependências (uv; --extra whatsapp inclui o webhook)
	uv sync --extra whatsapp

up:             ## sobe o Postgres
	docker compose up -d

down:
	docker compose down

db-init:        ## cria as tabelas
	uv run python scripts/init_db.py

seed:           ## cadastra a família de exemplo (edite scripts/seed_familia.py ou use flags)
	uv run python scripts/seed_familia.py --exemplo

run:            ## inicia o mordomo (Telegram + webhook do WhatsApp, se configurado)
	uv run python -m mordomo.main

test:           ## testes rápidos (sem rede, sem chaves)
	uv run pytest -q

evals:          ## eval de datas pt-BR (grátis); com --com-llm roda roteamento também
	uv run python evals/run_evals.py

lint:
	uv run ruff check src tests

replay:         ## como as respostas reais ficariam no WhatsApp (passo 0 da fase 3)
	uv run python scripts/replay_whatsapp.py --exemplo
