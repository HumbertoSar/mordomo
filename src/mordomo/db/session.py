"""Engine e sessões assíncronas (SQLAlchemy 2.0).

DATABASE_URL decide o banco: Postgres (padrão, igual produção) ou
sqlite+aiosqlite:// (testes). O checkpointer do LangGraph usa a MESMA
instância Postgres (ver main.py) — um banco só para estado, domínio e
eventos de analytics no MVP."""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ..config import settings
from .models import Base

# NullPool no SQLite: conexões aiosqlite não podem cruzar event loops (testes)
_kwargs = {"poolclass": NullPool} if settings.database_url.startswith("sqlite") else {}
engine = create_async_engine(settings.database_url, echo=False, **_kwargs)
Sessao = async_sessionmaker(engine, expire_on_commit=False)


async def criar_tabelas() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
