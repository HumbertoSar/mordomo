"""Efeitos mutantes por turno — a trava do retry, em memória.

O pipeline só repete um turno que falhou se NENHUMA tool mutante concluiu
(senão duplicaria lembrete/evento/item do cofre). O funil (product_events)
responde isso, mas ele é gravado com try/except deliberado — "falha de
analytics nunca derruba a conversa" — então justamente no cenário de banco
piscando, o tool_result ok=True pode se perder e o retry duplicaria o efeito.

Este registro é a fonte que NÃO depende do banco: `analytics.emitir` observa
aqui ANTES de tentar gravar, e o pipeline consulta memória + funil. Vive por
processo e por turno (o pipeline limpa ao fechar o turno) — retry acontece
sempre no mesmo processo, então é suficiente.

Tool mutante nova? Adicione em TOOLS_MUTANTES — o comentário-sentinela em
tools/ aponta para cá."""

TOOLS_MUTANTES = frozenset({
    "criar_lembrete",
    "cancelar_lembrete",
    # `preparar_evento` NÃO entra: preparar não grava em agenda nenhuma, e
    # bloquear o retry por causa dela deixaria o turno sem resposta. Quem cria
    # é `confirmar_evento` — e a proposta reivindicada é a segunda trava.
    "confirmar_evento",
    "guardar_info",
    "apagar_info",
    "criar_tarefa",
    "concluir_tarefa",
    "cancelar_tarefa",
    "reabrir_tarefa",
})

_turnos_com_efeito: set[str] = set()


def observar(tipo: str, turn_id: str | None, payload: dict) -> None:
    """Chamado por analytics.emitir para TODO evento; filtra o que importa."""
    if (
        tipo == "tool_result"
        and turn_id
        and payload.get("ok") is True
        and payload.get("tool") in TOOLS_MUTANTES
    ):
        _turnos_com_efeito.add(turn_id)


def houve_efeito(turn_id: str) -> bool:
    return turn_id in _turnos_com_efeito


def limpar(turn_id: str) -> None:
    """O pipeline chama ao fechar o turno — o set não cresce sem teto."""
    _turnos_com_efeito.discard(turn_id)
