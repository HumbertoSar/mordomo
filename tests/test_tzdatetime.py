"""TZDateTime: todo datetime lido do banco volta timezone-aware.

O SQLite (este teste roda nele — é o ponto) devolvia naive e quebrava
comparações com aware: `scheduler.carregar_pendentes` explodia no boot em modo
dev, e `astimezone` interpretava o valor como hora local da máquina."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from mordomo.db.models import Member, Reminder
from mordomo.db.session import Sessao


async def test_datetime_lido_do_banco_e_aware_e_comparavel():
    async with Sessao() as s:
        m = Member(nome="TzAware", papel="adulto")
        s.add(m)
        await s.flush()
        s.add(Reminder(member_id=m.id, texto="teste tz",
                       quando_utc=datetime.now(UTC) - timedelta(minutes=5)))
        await s.commit()

    # Sessão NOVA: o valor vem do banco, não do cache de identidade
    async with Sessao() as s:
        res = await s.execute(
            select(Reminder).join(Member).where(Member.nome == "TzAware")
        )
        lembrete = res.scalar_one()

    assert lembrete.quando_utc.tzinfo is not None
    # A comparação que explodia no carregar_pendentes (naive <= aware):
    assert lembrete.quando_utc <= datetime.now(UTC)
    # E o astimezone que exibia hora errada agora parte de UTC de verdade:
    diferenca = datetime.now(UTC) - lembrete.quando_utc
    assert timedelta(minutes=4) < diferenca < timedelta(minutes=6)
