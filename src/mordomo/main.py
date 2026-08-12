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
from .privacidade import carregar_segredos_do_cofre

log = logging.getLogger("mordomo")


def validar_ambiente() -> None:
    """Roda ANTES de qualquer conexão: erro de configuração tem que aparecer
    como uma frase, não como traceback de banco inacessível."""
    if not (settings.telegram_bot_token or settings.whatsapp_habilitado):
        sys.exit(
            "Nenhum canal configurado: defina TELEGRAM_BOT_TOKEN (@BotFather) "
            "e/ou as 4 variáveis do WhatsApp (docs/whatsapp-fase3.md)."
        )
    if not settings.openrouter_api_key:
        sys.exit("Defina OPENROUTER_API_KEY no .env (https://openrouter.ai/keys).")
    # Meia configuração do WhatsApp é armadilha: o webhook responderia 200 e a
    # resposta nunca sairia. Diz QUAL variável falta, não "algo deu errado".
    parciais = {
        "WHATSAPP_TOKEN": settings.whatsapp_token,
        "WHATSAPP_PHONE_NUMBER_ID": settings.whatsapp_phone_number_id,
        "WHATSAPP_APP_SECRET": settings.whatsapp_app_secret,
        "WHATSAPP_VERIFY_TOKEN": settings.whatsapp_verify_token,
    }
    faltando = [nome for nome, valor in parciais.items() if not valor]
    if faltando and len(faltando) < len(parciais):
        sys.exit(
            "WhatsApp configurado pela metade — falta: "
            + ", ".join(faltando)
            + " (ver docs/whatsapp-fase3.md)."
        )


async def main() -> None:
    if not settings.database_url.startswith("postgresql"):
        # SQLite (dev/teste): sem migrações, o create_all resolve.
        await criar_tabelas()
    checar_langfuse()  # falha de observabilidade tem que aparecer no boot, não em setembro
    # ANTES do primeiro turno: o histórico do checkpointer pode reenviar valores
    # do cofre ao trace, e a máscara (ADR-005) zera a cada restart.
    await carregar_segredos_do_cofre()

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

        # Canais em PARALELO durante a migração (fase 3): mesmo processo, mesmo
        # grafo, mesmo checkpointer — thread = MEMBRO (ADR-003), então quem
        # migrar do Telegram para o WhatsApp continua a MESMA conversa. Os
        # adapters se registram no notify antes de o scheduler disparar.
        canais = []
        if settings.telegram_bot_token:
            canais.append(TelegramAdapter(grafo))
        if settings.whatsapp_habilitado:
            from .channels.whatsapp import WhatsAppAdapter

            canais.append(WhatsAppAdapter(grafo))

        await scheduler.carregar_pendentes()
        scheduler.agendar_briefing()
        scheduler.agendar_curadoria()

        log.info("🤵 Mordomo a postos (canais: %s).", ", ".join(c.caps.canal for c in canais))
        # Cada start() bloqueia (long polling / servidor do webhook); o gather
        # mantém os dois no MESMO event loop.
        await asyncio.gather(*(canal.start() for canal in canais))


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
