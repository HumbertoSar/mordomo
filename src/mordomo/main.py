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
from .db.session import criar_tabelas
from .plataforma import preparar

log = logging.getLogger("mordomo")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if not settings.telegram_bot_token:
        sys.exit("Defina TELEGRAM_BOT_TOKEN no .env (crie o bot com o @BotFather).")
    if not settings.openrouter_api_key:
        sys.exit("Defina OPENROUTER_API_KEY no .env (https://openrouter.ai/keys).")

    await criar_tabelas()

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
    asyncio.run(main())
