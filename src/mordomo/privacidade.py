"""Mascaramento de segredos nos traces (ADR-005, item 2).

Toda string que as tools do Cofre leem ou gravam passa por
`registrar_segredo()`; o cliente Langfuse é criado com `mask=mascarar`, que
substitui qualquer ocorrência por «cofre» em qualquer canto do trace — input,
output, tool result, metadata.

Por que uma LISTA de valores e não regex de PII: regex de CPF/CEP erra nos
dois sentidos (deixa passar formato criativo, censura número inocente). Aqui o
usuário DISSE que é segredo ao guardar no Cofre — certeza, não heurística.

O registro vive em memória por processo (deque limitada). Reiniciou o bot,
recomeça vazio e volta a crescer conforme o Cofre é usado — aceitável: o risco
coberto é o vazamento contínuo via telemetria, não uma janela de boot."""

from collections import deque

_MASCARA = "«cofre»"
# 500 valores ≈ anos de uso de família; limite para nunca crescer sem teto
_segredos: deque[str] = deque(maxlen=500)


def registrar_segredo(valor: str | None) -> None:
    if not valor:
        return
    valor = str(valor).strip()
    # Curto demais mascaria substrings inocentes ("11" apagaria todo "11" dos traces)
    if len(valor) < 4 or valor in _segredos:
        return
    _segredos.append(valor)


def mascarar(data, **kwargs):  # assinatura exigida pelo mask do Langfuse
    """Substitui segredos registrados em qualquer estrutura (str/dict/list)."""
    if isinstance(data, str):
        for s in _segredos:
            if s in data:
                data = data.replace(s, _MASCARA)
        return data
    if isinstance(data, dict):
        return {k: mascarar(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return type(data)(mascarar(v) for v in data)
    return data
