"""Retry seguro do turno (caso real 11/08: OpenRouter devolve choices=None).

Regra: repetir o turno SÓ quando nenhuma tool mutante rodou — o funil é a
fonte de verdade. Com efeito colateral, nada de repetir: mensagem honesta.

Aqui também mora a serialização por thread: turnos da MESMA thread do
checkpointer nunca correm em paralelo (o último a escrever apagaria o outro
do histórico); threads diferentes seguem concorrentes."""

import asyncio
from datetime import UTC, datetime

from langchain_core.messages import AIMessage

from mordomo.analytics import emitir
from mordomo.channels.contract import InboundMessage
from mordomo.core.pipeline import _turno_teve_efeito, processar_entrada
from mordomo.db.models import Member


class _GrafoFalhaUmaVez:
    def __init__(self):
        self.chamadas = 0

    async def ainvoke(self, entrada, cfg):
        self.chamadas += 1
        if self.chamadas == 1:
            raise TypeError("'NoneType' object is not iterable")  # o erro real
        return {"messages": [AIMessage("Às ordens!")]}


class _GrafoSempreFalha:
    async def ainvoke(self, entrada, cfg):
        # simula o crash DEPOIS de uma tool mutante ter rodado
        turn_id = cfg["configurable"]["turn_id"]
        await emitir("tool_result", 1, "1:t", turn_id, tool="criar_lembrete", ok=True)
        raise TypeError("'NoneType' object is not iterable")


def _inbound() -> InboundMessage:
    return InboundMessage(member_id=1, canal="telegram", texto="oi",
                          message_id="1", timestamp=datetime.now(UTC))


def _membro(id: int = 990) -> Member:
    m = Member(nome="Retry", papel="adulto")
    m.id = id
    return m


async def test_crash_sem_efeito_repete_em_silencio_e_responde():
    grafo = _GrafoFalhaUmaVez()
    _, respostas = await processar_entrada(_membro(), _inbound(), grafo)
    assert grafo.chamadas == 2
    assert respostas[0].texto == "Às ordens!"  # o usuário nem percebe


async def test_crash_com_efeito_nao_repete_e_avisa_honestamente():
    _, respostas = await processar_entrada(_membro(), _inbound(), _GrafoSempreFalha())
    assert "Fiz o que você pediu" in respostas[0].texto  # não é o "Ops" genérico


async def test_turno_sem_eventos_nao_tem_efeito():
    assert await _turno_teve_efeito("turno-que-nao-existe") is False


async def test_tool_de_leitura_nao_conta_como_efeito():
    await emitir("tool_result", 1, "1:t", "t-leitura", tool="listar_lembretes", ok=True)
    assert await _turno_teve_efeito("t-leitura") is False


class _GrafoMedeConcorrencia:
    """Conta quantas invocações estão ativas AO MESMO TEMPO."""

    def __init__(self):
        self.ativos = 0
        self.pico = 0

    async def ainvoke(self, entrada, cfg):
        self.ativos += 1
        self.pico = max(self.pico, self.ativos)
        await asyncio.sleep(0.05)  # janela para a outra task entrar, se puder
        self.ativos -= 1
        return {"messages": [AIMessage("ok")]}


async def test_turnos_do_mesmo_membro_sao_serializados():
    grafo = _GrafoMedeConcorrencia()
    membro = _membro(991)
    await asyncio.gather(
        processar_entrada(membro, _inbound(), grafo),
        processar_entrada(membro, _inbound(), grafo),
    )
    assert grafo.pico == 1  # o 2º turno esperou o 1º soltar a thread


async def test_membros_diferentes_seguem_em_paralelo():
    """O lock é por THREAD, não global — senão a família inteira viraria fila."""
    grafo = _GrafoMedeConcorrencia()
    await asyncio.gather(
        processar_entrada(_membro(992), _inbound(), grafo),
        processar_entrada(_membro(993), _inbound(), grafo),
    )
    assert grafo.pico == 2
