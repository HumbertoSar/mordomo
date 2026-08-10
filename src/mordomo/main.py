"""Entrada do Mordomo: banco → checkpointer → grafo → scheduler → Telegram.

Rodar: `make run` (ou `uv run python -m mordomo.main`)."""

import asyncio
import logging
import sys
from contextlib import AsyncExitStack

from . import scheduler
from .channels.telegram import TelegramAdapter
from .config import settings
from .core.graph import build_graph
from .db.migracoes import aplicar_migracoes
from .db.session import criar_tabelas
from .observability import checar_langfuse
from .plataforma import preparar

log = logging.getLogger("mordomo")


def validar_ambiente() -> None:
    """Roda ANTES de qualquer conexão: erro de configuração tem que aparecer
    como uma frase, não como traceback de banco inacessível."""
    if not settings.telegram_bot_token:
        sys.exit("Defina TELEGRAM_BOT_TOKEN no .env (crie o bot com o @BotFather).")
    if not settings.openrouter_api_key:
        sys.exit("Defina OPENROUTER_API_KEY no .env (https://openrouter.ai/keys).")


async def main() -> None:
    if not settings.database_url.startswith("postgresql"):
        # SQLite (dev/teste): sem migrações, o create_all resolve.
        await criar_tabelas()
    checar_langfuse()  # falha de observabilidade tem que aparecer no boot, não em setembro

    async with AsyncExitStack() as stack:
        # Checkpointer: estado da conversa por membro (thread = membro, ADR-003)
        if settings.database_url.startswith("postgresql"):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            checkpointer = await stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(settings.checkpointer_conn_string)
            )
            await checkpointer.setup()
        else:
            from langgraph.checkpoint.memory import InMemorySaver

            checkpointer = InMemorySaver()
            log.warning("Sem Postgres: histórico de conversa NÃO sobrevive a restart")

        grafo = build_graph(checkpointer)

        scheduler.iniciar()
        adapter = TelegramAdapter(grafo)  # registra-se no notify antes do scheduler disparar
        await scheduler.carregar_pendentes()

        log.info("🤵 Mordomo a postos.")
        await adapter.start()


if __name__ == "__main__":
    preparar()  # event loop + UTF-8 (Windows) — antes do asyncio.run
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    validar_ambiente()
    if settings.database_url.startswith("postgresql"):
        # Fora do asyncio.run de propósito: o env.py do Alembic abre o próprio
        # event loop e não pode ser chamado de dentro de um já rodando.
        aplicar_migracoes()
    asyncio.run(main())
