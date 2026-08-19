"""Config dos testes: SQLite em arquivo temporário (sem Docker, sem chaves).

IMPORTANTE: as variáveis de ambiente são definidas ANTES de importar mordomo,
porque config.Settings lê o ambiente no import."""

import asyncio
import os
import pathlib

# SQLite hermético por padrão. Uma execução de contrato pode injetar um
# PostgreSQL efêmero explicitamente; nunca apontar isto para produção.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./.pytest_mordomo.db")
os.environ.setdefault("TZ_FAMILIA", "America/Sao_Paulo")
# Hermético de verdade: variável de ambiente VAZIA vence o .env no
# pydantic-settings — mesmo com chaves reais no .env local, os testes rodam
# sem Langfuse e sem OpenRouter (regra nº 7 do projeto).
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["OPENROUTER_API_KEY"] = ""

import pytest


@pytest.fixture(scope="session", autouse=True)
def banco_de_teste():
    from mordomo.db.session import criar_tabelas, engine

    asyncio.run(criar_tabelas())
    yield
    asyncio.run(engine.dispose())
    pathlib.Path(".pytest_mordomo.db").unlink(missing_ok=True)
