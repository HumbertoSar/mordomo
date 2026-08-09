"""Cria as tabelas no banco apontado por DATABASE_URL. Uso: make db-init"""

import asyncio

from mordomo.db.session import criar_tabelas, engine
from mordomo.plataforma import preparar


async def main() -> None:
    await criar_tabelas()
    print(f"Tabelas criadas em {engine.url.render_as_string(hide_password=True)}")


if __name__ == "__main__":
    preparar()  # psycopg async não roda no ProactorEventLoop do Windows
    asyncio.run(main())
