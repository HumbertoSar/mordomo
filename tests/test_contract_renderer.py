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
    fatiar_texto,
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


# ── fatiar_texto: acima de max_texto o canal REJEITA a mensagem inteira ──


def test_texto_curto_nao_e_fatiado():
    assert fatiar_texto("oi", 4096) == ["oi"]


def test_texto_vazio_vira_um_bloco_unico():
    assert fatiar_texto("", 4096) == [""]


def test_fatia_prefere_fim_de_linha():
    linhas = "\n".join(f"• lembrete {i}" for i in range(10))
    partes = fatiar_texto(linhas, 40)
    assert all(len(p) <= 40 for p in partes)
    # Nenhuma linha foi cortada no meio: cada bloco recompõe linhas inteiras
    assert [l for p in partes for l in p.split("\n")] == linhas.split("\n")


def test_paragrafo_maior_que_o_limite_leva_corte_seco():
    partes = fatiar_texto("x" * 100, 40)
    assert partes == ["x" * 40, "x" * 40, "x" * 20]


def test_quebra_exatamente_no_limite_nao_gera_bloco_vazio():
    texto = "a" * 40 + "\n" + "b" * 10
    partes = fatiar_texto(texto, 40)
    assert partes == ["a" * 40, "b" * 10]
    assert all(partes)  # o Telegram rejeita mensagem vazia
