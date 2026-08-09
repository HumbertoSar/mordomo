"""Ajustes de plataforma — dois consertos obrigatórios no Windows.

Ambos foram descobertos na PRIMEIRA execução real do projeto; nenhum aparece em
teste (o `make test` roda em SQLite e não imprime caixa nem emoji). É o loop que
este projeto ensina: executar, ver quebrar, consertar, documentar.

1. **Event loop.** Desde o Python 3.8 o Windows usa `ProactorEventLoop` por
   padrão, e o psycopg em modo assíncrono não roda nele:
   `InterfaceError: Psycopg cannot use the 'ProactorEventLoop'`. Sem esta
   troca, NADA que fale com o Postgres funciona no Windows — nem `init_db`,
   nem o seed, nem o bot. Precisa acontecer ANTES de `asyncio.run()`.

2. **Encoding.** O console do Windows usa cp1252: os ✓/✗ e ── dos evals e o 🤵
   dos logs estouram `UnicodeEncodeError`.

O `SelectorEventLoop` não suporta subprocessos e tem teto de 512 sockets —
irrelevante aqui (aiogram/aiohttp, psycopg e APScheduler rodam bem nele).
"""

import asyncio
import sys


def configurar_event_loop() -> None:
    """Troca o Proactor pelo Selector no Windows (exigência do psycopg async)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def forcar_utf8() -> None:
    """Saída de console em UTF-8 (o cp1252 do Windows não aceita ✓, ── nem 🤵)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigurar = getattr(stream, "reconfigure", None)
        if reconfigurar is not None:  # TextIOWrapper (Python 3.7+)
            reconfigurar(encoding="utf-8", errors="replace")


def preparar() -> None:
    """Chame no ponto de entrada, ANTES de `asyncio.run()`."""
    configurar_event_loop()
    forcar_utf8()
