"""Golden tests da degradação de renderização (ADR-001).

Estes testes são o CONTRATO da migração Telegram → WhatsApp: se passarem,
a mesma OutboundMessage semântica tem um plano válido nos dois canais."""

from mordomo.channels.contract import (
    TELEGRAM_CAPS,
    WHATSAPP_CAPS,
    Choice,
    ChoiceOption,
    Confirmation,
    RenderMode,
    plan_rendering,
    render_numbered_text,
)


def opcoes(n: int) -> Choice:
    return Choice(opcoes=[ChoiceOption(id=f"op{i}", rotulo=f"Opção {i}") for i in range(n)])


def test_informativo_vira_texto_puro_em_ambos():
    assert plan_rendering(None, TELEGRAM_CAPS) is RenderMode.PLAIN
    assert plan_rendering(None, WHATSAPP_CAPS) is RenderMode.PLAIN


def test_confirmacao_vira_botoes_em_ambos():
    assert plan_rendering(Confirmation(), TELEGRAM_CAPS) is RenderMode.BUTTONS
    assert plan_rendering(Confirmation(), WHATSAPP_CAPS) is RenderMode.BUTTONS


def test_3_opcoes_cabem_em_botoes_nos_dois_canais():
    assert plan_rendering(opcoes(3), TELEGRAM_CAPS) is RenderMode.BUTTONS
    assert plan_rendering(opcoes(3), WHATSAPP_CAPS) is RenderMode.BUTTONS


def test_8_opcoes_botoes_no_telegram_lista_no_whatsapp():
    assert plan_rendering(opcoes(8), TELEGRAM_CAPS) is RenderMode.BUTTONS
    assert plan_rendering(opcoes(8), WHATSAPP_CAPS) is RenderMode.LIST


def test_15_opcoes_degradam_para_texto_numerado_nos_dois():
    # Telegram: acima de max_buttons e sem list message → texto numerado
    assert plan_rendering(opcoes(15), TELEGRAM_CAPS) is RenderMode.NUMBERED_TEXT
    # WhatsApp: acima dos 10 itens da list message → texto numerado
    assert plan_rendering(opcoes(15), WHATSAPP_CAPS) is RenderMode.NUMBERED_TEXT


def test_fallback_numerado_e_utilizavel_por_texto():
    txt = render_numbered_text("Qual filme?", opcoes(3))
    assert "1. Opção 0" in txt
    assert "Responda com o número" in txt
