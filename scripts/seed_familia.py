"""Cadastra membros da família (MVP: sem fluxo /vincular ainda — fase 2).

Como descobrir o telegram_id de cada pessoa: peça para ela mandar /start ao
bot e veja o aviso de "unknown_user" no log/tabela product_events, ou use o
bot @userinfobot no Telegram.

Uso:
  uv run python scripts/seed_familia.py --nome "Humberto" --telegram-id 123456 --papel adulto
  uv run python scripts/seed_familia.py --exemplo   (família fictícia p/ testes)
"""

import argparse
import asyncio

from mordomo.db.models import ChannelIdentity, Member
from mordomo.db.session import Sessao, criar_tabelas


async def cadastrar(nome: str, telegram_id: str, papel: str) -> None:
    async with Sessao() as s:
        membro = Member(nome=nome, papel=papel)
        s.add(membro)
        await s.flush()
        s.add(ChannelIdentity(member_id=membro.id, canal="telegram", external_id=telegram_id))
        await s.commit()
        print(f"OK: {nome} (member_id={membro.id}, papel={papel}, telegram={telegram_id})")


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--nome")
    p.add_argument("--telegram-id")
    p.add_argument("--papel", default="adulto", choices=["adulto", "crianca"])
    p.add_argument("--exemplo", action="store_true", help="cadastra família fictícia")
    args = p.parse_args()

    await criar_tabelas()
    if args.exemplo:
        for nome, tid, papel in [
            ("Humberto", "111111111", "adulto"),
            ("Esposa", "222222222", "adulto"),
            ("Filho", "333333333", "crianca"),
        ]:
            await cadastrar(nome, tid, papel)
    elif args.nome and args.telegram_id:
        await cadastrar(args.nome, args.telegram_id, args.papel)
    else:
        p.error("use --nome + --telegram-id, ou --exemplo")


if __name__ == "__main__":
    asyncio.run(main())
