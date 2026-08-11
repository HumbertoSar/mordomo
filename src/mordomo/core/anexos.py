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
    """Esvazia e devolve os anexos do turno, sem repetição.

    O retry do pipeline reexecuta o turno inteiro: buscar_documento roda de
    novo e registra o MESMO documento outra vez — sem o dedupe, o usuário
    receberia o RG em dobro."""
    anexos = _pendentes.pop(turn_id, [])
    vistos: set[int] = set()
    unicos: list[Anexo] = []
    for a in anexos:
        if a.documento_id not in vistos:
            vistos.add(a.documento_id)
            unicos.append(a)
    return unicos
