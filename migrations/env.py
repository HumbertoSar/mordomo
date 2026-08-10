import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from mordomo.config import settings
from mordomo.db.models import Base
from mordomo.plataforma import preparar

# psycopg async não roda no ProactorEventLoop do Windows (ver plataforma.py):
# vale para o alembic exatamente como vale para o bot.
preparar()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# A URL vem do .env (uma fonte de verdade só), nunca do alembic.ini — assim o
# arquivo pode ir para o repositório público sem carregar credencial.
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# ARMADILHA REAL: o checkpointer do LangGraph cria e gerencia as próprias tabelas
# (checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations) via
# `checkpointer.setup()`, no MESMO banco. Elas não estão no nosso Base.metadata,
# então o autogenerate as enxerga como "tabelas removidas" e escreve drop_table
# para cada uma — o que apagaria TODO o histórico de conversas da família na
# primeira migração. Aconteceu aqui, na primeira execução do autogenerate.
_TABELAS_DE_TERCEIROS = ("checkpoint", "alembic_version")


def include_object(objeto, nome, tipo, reflexo, comparador):
    alvo = nome or ""
    if tipo == "table":
        return not alvo.startswith(_TABELAS_DE_TERCEIROS)
    if tipo == "index":
        return not (getattr(objeto, "table", None) is not None
                    and str(objeto.table.name).startswith(_TABELAS_DE_TERCEIROS))
    return True

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
