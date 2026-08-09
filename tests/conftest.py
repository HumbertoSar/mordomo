"""Config dos testes: SQLite em arquivo temporário (sem Docker, sem chaves).

IMPORTANTE: as variáveis de ambiente são definidas ANTES de importar mordomo,
porque config.Settings lê o ambiente no import."""

import asyncio
import os
import pathlib

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./.pytest_mordomo.db"
os.environ.setdefault("TZ_FAMILIA", "America/Sao_Paulo")

import pytest


@pytest.fixture(scope="session", autouse=True)
def banco_de_teste():
    from mordomo.db.session import criar_tabelas, engine

    asyncio.run(criar_tabelas())
    yield
    asyncio.run(engine.dispose())
    pathlib.Path(".pytest_mordomo.db").unlink(missing_ok=True)
