"""Ponte tool → canal para anexos (documentos que a resposta deve levar).

O problema: tools devolvem TEXTO ao LLM; enviar uma imagem é ação de CANAL.
Confiar num marcador no texto ("[DOC:17]") deixaria o envio refém de o modelo
copiar o marcador direitinho — frágil. Em vez disso, a tool registra o anexo
AQUI, por turn_id (que ela tem via config), e o pipeline coleta depois do grafo
e monta o OutboundMessage. O LLM só descreve; quem anexa é o sistema.

Registro em memória por processo, esvaziado a cada turno pelo próprio coletar()."""

from ..channels.contract import Anexo

_pendentes: dict[str, list[Anexo]] = {}


def registrar(config, anexo: Anexo) -> None:
    turn_id = (config.get("configurable", {}) or {}).get("turn_id") if isinstance(config, dict) else None
    if not turn_id:
        return  # sem turno (ex.: chamada avulsa em teste) → nada a entregar
    _pendentes.setdefault(turn_id, []).append(anexo)


def coletar(turn_id: str) -> list[Anexo]:
    return _pendentes.pop(turn_id, [])
