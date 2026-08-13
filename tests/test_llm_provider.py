"""Roteamento de provedor do OpenRouter — o incidente de 13/08/2026.

O supervisor usa structured output. O MESMO modelo servido por hosts
diferentes não tem as mesmas capacidades: Bedrock e Azure ignoram
`response_format: json_schema` para `anthropic/*` e devolvem texto comum, o
que estoura o parser e deixa o bot mudo nos dois canais. Estes testes fixam a
forma do payload que impede isso de voltar."""

from mordomo.config import settings
from mordomo.core.llm import chat_model


def _provider(modelo) -> dict:
    return (modelo.extra_body or {})["provider"]


def test_require_parameters_vai_sempre(monkeypatch):
    """Barra provedor que não aceita os parâmetros da requisição."""
    monkeypatch.setattr(settings, "openrouter_provider_order", "")
    monkeypatch.setattr(settings, "openrouter_provider_sort", "")
    assert _provider(chat_model("anthropic/claude-haiku-4.5"))["require_parameters"] is True


def test_ordem_de_provedores_trava_e_desliga_fallback(monkeypatch):
    """Com ordem definida, nada de cair em quem só finge suportar schema."""
    monkeypatch.setattr(settings, "openrouter_provider_order", "anthropic, google")
    p = _provider(chat_model("anthropic/claude-haiku-4.5"))
    assert p["order"] == ["anthropic", "google"]
    assert p["allow_fallbacks"] is False
    # sort não convive com order: ordenar por latência é o que trazia o Bedrock
    assert "sort" not in p


def test_sem_ordem_mantem_a_preferencia_de_latencia(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_provider_order", "")
    monkeypatch.setattr(settings, "openrouter_provider_sort", "latency")
    p = _provider(chat_model("anthropic/claude-haiku-4.5"))
    assert p["sort"] == "latency" and "order" not in p
