"""Boot: o que precisa estar certo ANTES do primeiro turno.

ADR-005 pós-restart: a máscara de traces é reposta a partir do cofre — o
histórico do checkpointer reenvia valores antigos ao LLM/trace e eles não podem
sair em claro só porque o processo reiniciou. E `validar_ambiente`: configuração
incoerente tem que virar uma frase no boot, não um mistério em produção."""

import pytest

from mordomo import privacidade
from mordomo.config import settings
from mordomo.db.models import Member, VaultItem
from mordomo.db.session import Sessao
from mordomo.main import validar_ambiente
from mordomo.privacidade import carregar_segredos_do_cofre, mascarar

WHATSAPP = (
    "whatsapp_token",
    "whatsapp_phone_number_id",
    "whatsapp_app_secret",
    "whatsapp_verify_token",
)
GOOGLE = (
    "google_client_id",
    "google_client_secret",
    "google_redirect_uri",
    "google_token_key",
)


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


# ── validar_ambiente: o piloto Google depende da borda HTTP do WhatsApp ──


def _so_telegram(monkeypatch):
    """Ambiente mínimo que hoje sobe: Telegram + LLM, sem WhatsApp e sem
    Google."""
    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-de-teste")
    for campo in WHATSAPP + GOOGLE:
        monkeypatch.setattr(settings, campo, "")
    return settings


def _ligar(monkeypatch, campos):
    for campo in campos:
        monkeypatch.setattr(settings, campo, "valor-de-teste")


def test_google_ligado_sem_whatsapp_para_o_boot_com_mensagem_util(monkeypatch):
    """O callback do OAuth mora no FastAPI do WhatsApp (ADR-010). Sem essa
    borda no ar, /google entrega um link que não tem para onde voltar — e o
    sintoma apareceria só na cara da família, no meio do consentimento."""
    _so_telegram(monkeypatch)
    _ligar(monkeypatch, GOOGLE)

    with pytest.raises(SystemExit) as saida:
        validar_ambiente()

    texto = str(saida.value)
    assert "GOOGLE" in texto and "WhatsApp" in texto
    assert "callback" in texto.lower()
    # e diz o que fazer, não só o que está errado
    assert "docs/google-calendar-piloto.md" in texto


def test_google_com_whatsapp_no_ar_deixa_o_boot_seguir(monkeypatch):
    _so_telegram(monkeypatch)
    _ligar(monkeypatch, WHATSAPP + GOOGLE)
    assert validar_ambiente() is None


def test_google_vazio_nao_atrapalha_quem_nao_usa(monkeypatch):
    """Regra do projeto: sem as variáveis, o bot sobe igual."""
    _so_telegram(monkeypatch)
    assert validar_ambiente() is None
