"""Mascaramento de segredos nos traces (ADR-005, item 2).

Toda string que as tools do Cofre leem ou gravam passa por
`registrar_segredo()`; o cliente Langfuse é criado com `mask=mascarar`, que
substitui qualquer ocorrência por «cofre» em qualquer canto do trace — input,
output, tool result, metadata.

Por que uma LISTA de valores e não regex de PII: regex de CPF/CEP erra nos
dois sentidos (deixa passar formato criativo, censura número inocente). Aqui o
usuário DISSE que é segredo ao guardar no Cofre — certeza, não heurística.

O registro vive em memória por processo (deque limitada) e é REPOSTO no boot
por `carregar_segredos_do_cofre()`: sem isso, após um restart o histórico do
checkpointer reenviaria valores antigos do Cofre ao LLM — e portanto ao trace
do Langfuse — sem máscara, até alguém usar o Cofre de novo."""

import logging
from collections import deque

log = logging.getLogger(__name__)

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


async def carregar_segredos_do_cofre() -> int:
    """Repõe a máscara com o que já está no Cofre — chamar no boot (main.py).

    Falha aqui não pode impedir o bot de subir (mesma filosofia do Langfuse
    desligado): logamos e seguimos — a máscara volta a crescer com o uso."""
    from sqlalchemy import select

    from .db.models import VaultItem
    from .db.session import Sessao

    try:
        async with Sessao() as s:
            res = await s.execute(select(VaultItem.valor))
            valores = [v for (v,) in res.all()]
    except Exception:
        log.exception("Não deu para repor a máscara do cofre no boot (seguindo sem)")
        return 0
    for valor in valores:
        registrar_segredo(valor)
    log.info("Máscara de traces reposta: %d segredo(s) do cofre", len(valores))
    return len(valores)


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
