"""notify.notificar: falha de canal vira False, nunca exceção.

O scheduler assume esse contrato para marcar o lembrete como "falhou" (em vez
de deixá-lo "pendente" para sempre com o job já consumido), e a curadoria para
seguir ao próximo adulto quando um deles bloqueou o bot."""

from sqlalchemy import select

from mordomo import notify
from mordomo.channels.contract import TELEGRAM_CAPS
from mordomo.db.models import ChannelIdentity, Member, ProductEvent
from mordomo.db.session import Sessao


class _AdapterQueExplode:
    caps = TELEGRAM_CAPS

    async def enviar(self, member_id, msg) -> None: ...

    async def notificar(self, member_id, texto) -> None:
        raise RuntimeError("usuário bloqueou o bot")


class _AdapterQueFunciona:
    caps = TELEGRAM_CAPS
    enviadas: list[tuple[int, str]]

    def __init__(self) -> None:
        self.enviadas = []

    async def enviar(self, member_id, msg) -> None: ...

    async def notificar(self, member_id, texto) -> None:
        self.enviadas.append((member_id, texto))


async def _membro_com_telegram(nome: str, external_id: str) -> Member:
    async with Sessao() as s:
        m = Member(nome=nome, papel="adulto")
        s.add(m)
        await s.flush()
        s.add(ChannelIdentity(member_id=m.id, canal="telegram", external_id=external_id))
        await s.commit()
        await s.refresh(m)
        return m


async def test_falha_de_envio_vira_false_nunca_excecao(monkeypatch):
    monkeypatch.setattr(notify, "_adapters", {"telegram": _AdapterQueExplode()})
    membro = await _membro_com_telegram("NotifyBloqueou", "99001")
    assert await notify.notificar(membro.id, "⏰ Lembrete: remédio") is False


async def test_membro_sem_canal_vira_false(monkeypatch):
    monkeypatch.setattr(notify, "_adapters", {"telegram": _AdapterQueFunciona()})
    async with Sessao() as s:
        m = Member(nome="NotifySemCanal", papel="adulto")
        s.add(m)
        await s.commit()
        await s.refresh(m)
    assert await notify.notificar(m.id, "oi") is False


async def test_canal_preferido_falhou_cai_para_o_proximo(monkeypatch):
    """Caso real da migração (13/08): WhatsApp vinculado mas barrado pela Meta
    (erro 130497). Sem fallback, o membro com os DOIS canais parava de receber
    lembretes — com o Telegram de pé, ao lado, funcionando."""
    from mordomo.channels.contract import WHATSAPP_CAPS
    from mordomo.config import settings

    class _WhatsAppQueFalha(_AdapterQueExplode):
        caps = WHATSAPP_CAPS

    telegram = _AdapterQueFunciona()
    monkeypatch.setattr(
        notify, "_adapters", {"telegram": telegram, "whatsapp": _WhatsAppQueFalha()}
    )
    monkeypatch.setattr(settings, "canal_preferido", "whatsapp")

    membro = await _membro_com_telegram("NotifyFallback", "99003")
    async with Sessao() as s:
        s.add(ChannelIdentity(member_id=membro.id, canal="whatsapp", external_id="5521999003"))
        await s.commit()

    assert await notify.notificar(membro.id, "⏰ remédio do Davi") is True
    assert telegram.enviadas == [(membro.id, "⏰ remédio do Davi")]  # chegou pelo Telegram

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(
                ProductEvent.tipo == "proactive_sent", ProductEvent.member_id == membro.id
            )
        )
        evento = res.scalars().one()
    assert evento.payload["canal"] == "telegram"
    assert evento.payload["fallback_de"] == ["whatsapp"]  # o rastro da migração


async def test_todos_os_canais_falharam_vira_false_e_evento_proprio(monkeypatch):
    from mordomo.channels.contract import WHATSAPP_CAPS

    class _WhatsAppQueFalha(_AdapterQueExplode):
        caps = WHATSAPP_CAPS

    monkeypatch.setattr(
        notify, "_adapters",
        {"telegram": _AdapterQueExplode(), "whatsapp": _WhatsAppQueFalha()},
    )
    membro = await _membro_com_telegram("NotifyTudoFalhou", "99004")
    async with Sessao() as s:
        s.add(ChannelIdentity(member_id=membro.id, canal="whatsapp", external_id="5521999004"))
        await s.commit()

    assert await notify.notificar(membro.id, "oi") is False
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(
                ProductEvent.tipo == "proactive_failed", ProductEvent.member_id == membro.id
            )
        )
        evento = res.scalars().one()
    assert sorted(evento.payload["canais"]) == ["telegram", "whatsapp"]


async def test_envio_ok_devolve_true_e_emite_proactive_sent(monkeypatch):
    adapter = _AdapterQueFunciona()
    monkeypatch.setattr(notify, "_adapters", {"telegram": adapter})
    membro = await _membro_com_telegram("NotifyOk", "99002")

    assert await notify.notificar(membro.id, "bom dia!") is True
    assert adapter.enviadas == [(membro.id, "bom dia!")]

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(
                ProductEvent.tipo == "proactive_sent", ProductEvent.member_id == membro.id
            )
        )
        eventos = list(res.scalars())
    assert len(eventos) == 1
