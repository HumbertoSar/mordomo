"""Preços por modelo, em USD por 1 MILHÃO de tokens.

Por que existe uma tabela local: o cálculo automático de custo do Langfuse nem
sempre reconhece os ids de modelo do OpenRouter (gotcha conhecido do projeto).
Com fonte própria o dashboard não depende disso — e o custo é recalculável para
todo o histórico quando um preço muda, porque `product_events` só guarda tokens.

Conferido em 10/08/2026 na API pública do OpenRouter. Para atualizar:
    uv run python -m mordomo.reporting.precos
"""

# WhatsApp Cloud API: template categoria UTILITY entregue no BRASIL, em USD
# por MENSAGEM (a Meta cobra por mensagem entregue desde 07/2025; texto livre
# dentro da janela de 24h é grátis). Conferido em 08/2026 — se a Meta revisar
# a tabela, o histórico recalcula sozinho, como no custo de LLM.
PRECO_TEMPLATE_WHATSAPP_USD = 0.0068

# modelo -> (entrada, saída) em USD por 1M de tokens
PRECOS: dict[str, tuple[float, float]] = {
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "anthropic/claude-sonnet-4.5": (3.00, 15.00),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "openai/gpt-4o-mini": (0.15, 0.60),
}


def custo_usd(modelo: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """Custo de uma chamada. None quando o modelo não está na tabela — melhor
    admitir que não sabe do que somar zero e mentir no total."""
    preco = PRECOS.get(modelo or "")
    if preco is None:
        return None
    entrada, saida = preco
    return (input_tokens / 1_000_000) * entrada + (output_tokens / 1_000_000) * saida


def _refrescar() -> None:  # pragma: no cover - utilitário manual
    """Imprime a tabela atualizada a partir da API do OpenRouter, para colar acima."""
    import json
    import urllib.request

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models", headers={"User-Agent": "mordomo"}
    )
    dados = json.load(urllib.request.urlopen(req, timeout=30))["data"]
    por_id = {m["id"]: m.get("pricing", {}) for m in dados}
    print("PRECOS: dict[str, tuple[float, float]] = {")
    for modelo, atual in PRECOS.items():
        p = por_id.get(modelo)
        if not p:
            print(f'    "{modelo}": {atual},  # NÃO ENCONTRADO na API')
            continue
        entrada = float(p.get("prompt", 0)) * 1_000_000
        saida = float(p.get("completion", 0)) * 1_000_000
        print(f'    "{modelo}": ({entrada:.2f}, {saida:.2f}),')
    print("}")


if __name__ == "__main__":  # pragma: no cover
    _refrescar()
