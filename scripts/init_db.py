"""Cria as tabelas no banco apontado por DATABASE_URL. Uso: make db-init"""

import asyncio

from mordomo.db.session import criar_tabelas, engine


async def main() -> None:
    await criar_tabelas()
    print(f"Tabelas criadas em {engine.url.render_as_string(hide_password=True)}")


if __name__ == "__main__":
    asyncio.run(main())
