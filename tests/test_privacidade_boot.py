"""ADR-005 pós-restart: a máscara de traces é reposta no boot a partir do
cofre — o histórico do checkpointer reenvia valores antigos ao LLM/trace e
eles não podem sair em claro só porque o processo reiniciou."""

from mordomo import privacidade
from mordomo.db.models import Member, VaultItem
from mordomo.db.session import Sessao
from mordomo.privacidade import carregar_segredos_do_cofre, mascarar


async def test_boot_repoe_a_mascara_com_o_cofre():
    async with Sessao() as s:
        m = Member(nome="PrivBoot", papel="adulto")
        s.add(m)
        await s.flush()
        s.add(VaultItem(chave="RG do PrivBoot", valor="98.765.432-1", dono=m.id))
        await s.commit()

    privacidade._segredos.clear()  # simula o processo recém-nascido
    assert mascarar("meu RG é 98.765.432-1") == "meu RG é 98.765.432-1"  # sem máscara!

    carregados = await carregar_segredos_do_cofre()
    assert carregados >= 1
    assert "98.765.432-1" not in mascarar("meu RG é 98.765.432-1")


async def test_falha_de_banco_nao_derruba_o_boot(monkeypatch):
    class _SessaoQuebrada:
        def __call__(self):
            raise RuntimeError("banco fora no boot")

    monkeypatch.setattr("mordomo.db.session.Sessao", _SessaoQuebrada())
    assert await carregar_segredos_do_cofre() == 0  # loga e segue; não explode
