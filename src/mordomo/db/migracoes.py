"""Migrações (Alembic) aplicadas programaticamente.

Por que existe: `Base.metadata.create_all` cria tabelas que faltam mas NUNCA
altera as que existem. A partir do momento em que há dado real da família no
banco, toda mudança de schema (recorrência de lembretes, memória de longo prazo,
tarefas) precisa de migração — e o momento mais barato de adotar Alembic é o
anterior ao primeiro `ALTER`.

Atenção ao rodar: `command.upgrade` é síncrono e o `migrations/env.py` abre o
próprio `asyncio.run`. Chamar isto de DENTRO de um event loop rodando estoura
"asyncio.run() cannot be called from a running event loop" — por isso os pontos
de entrada chamam antes do `asyncio.run(main())`, não dentro dele."""

import logging
import pathlib

log = logging.getLogger(__name__)

_RAIZ = pathlib.Path(__file__).resolve().parents[3]
_INI = _RAIZ / "alembic.ini"


def aplicar_migracoes() -> bool:
    """Leva o banco até a última revisão. Devolve False se o Alembic não está
    disponível (ex.: instalado como wheel, sem a pasta migrations/)."""
    if not _INI.exists():
        log.warning("alembic.ini não encontrado em %s — pulando migrações", _INI)
        return False

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_INI))
    cfg.set_main_option("script_location", str(_RAIZ / "migrations"))
    # Sem isto, o fileConfig do env.py aplicaria o logging do alembic.ini
    # (root em WARNING) ao processo inteiro e o bot seguiria mudo depois do boot.
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    return True
