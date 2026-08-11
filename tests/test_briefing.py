"""Briefing matinal: conteúdo do dia e a regra anti-spam (vazio = None)."""

from datetime import UTC, datetime, timedelta

from apoio import criar_membro as _membro
from mordomo.briefing import montar_briefing
from mordomo.db.models import FamilyEvent, Reminder
from mordomo.db.session import Sessao


async def test_dia_vazio_nao_gera_briefing():
    membro = await _membro("BriefingVazio")
    assert await montar_briefing(membro.id) is None


async def test_briefing_traz_lembrete_e_agenda_de_hoje():
    membro = await _membro("BriefingCheio")
    daqui_1h = datetime.now(UTC) + timedelta(hours=1)
    async with Sessao() as s:
        s.add(Reminder(member_id=membro.id, texto="tomar o remédio", quando_utc=daqui_1h))
        s.add(FamilyEvent(titulo="dentista do Davi", inicio_utc=daqui_1h, criado_por=membro.id))
        # Ruído que NÃO pode aparecer: lembrete de amanhã e já enviado de hoje
        s.add(Reminder(member_id=membro.id, texto="coisa de amanhã",
                       quando_utc=daqui_1h + timedelta(days=1)))
        s.add(Reminder(member_id=membro.id, texto="ja tocou", quando_utc=daqui_1h,
                       status="enviado"))
        await s.commit()

    texto = await montar_briefing(membro.id)
    assert texto is not None
    assert "tomar o remédio" in texto
    assert "dentista do Davi" in texto
    assert "coisa de amanhã" not in texto
    assert "ja tocou" not in texto


async def test_briefing_de_outro_membro_nao_ve_meus_lembretes():
    """Lembrete é PRIVADO; agenda é da família (ADR-003) — o briefing do outro
    membro pode trazer a agenda comum, mas nunca lembrete alheio."""
    dono = await _membro("BriefingDono")
    outro = await _membro("BriefingOutro")
    async with Sessao() as s:
        s.add(Reminder(member_id=dono.id, texto="segredo do dono",
                       quando_utc=datetime.now(UTC) + timedelta(hours=2)))
        await s.commit()

    briefing_do_outro = await montar_briefing(outro.id)
    assert briefing_do_outro is None or "segredo do dono" not in briefing_do_outro
    briefing_do_dono = await montar_briefing(dono.id)
    assert briefing_do_dono is not None and "segredo do dono" in briefing_do_dono