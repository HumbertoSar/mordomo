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
from mordomo.core.llm import LLMDeadlineExceeded
from mordomo.core.pipeline import _turno_teve_efeito, processar_entrada
from mordomo.db.models import Member
from mordomo.reporting.queries import _eventos


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


async def test_timeout_interno_imediato_ainda_usa_retry_seguro():
    class _GrafoTimeoutUmaVez:
        def __init__(self):
            self.chamadas = 0

        async def ainvoke(self, entrada, cfg):
            self.chamadas += 1
            if self.chamadas == 1:
                raise TimeoutError("timeout interno de uma dependência")
            return {"messages": [AIMessage("recuperou")]}

    grafo = _GrafoTimeoutUmaVez()
    _, respostas = await processar_entrada(_membro(999), _inbound(), grafo)

    assert grafo.chamadas == 2
    assert respostas[0].texto == "recuperou"


async def test_crash_com_efeito_nao_repete_e_avisa_honestamente():
    _, respostas = await processar_entrada(_membro(), _inbound(), _GrafoSempreFalha())
    assert "Fiz o que você pediu" in respostas[0].texto  # não é o "Ops" genérico


async def test_turno_sem_eventos_nao_tem_efeito():
    assert await _turno_teve_efeito("turno-que-nao-existe") is False


async def test_tool_de_leitura_nao_conta_como_efeito():
    await emitir("tool_result", 1, "1:t", "t-leitura", tool="listar_lembretes", ok=True)
    assert await _turno_teve_efeito("t-leitura") is False


class _GrafoFalhaComAnalyticsForaDoAr:
    """O cenário que o registro em memória (core/efeitos.py) existe para
    cobrir: a tool mutante RODOU, mas o banco de analytics engoliu o
    tool_result. Sem a memória, o retry duplicaria o lembrete."""

    def __init__(self):
        self.chamadas = 0

    async def ainvoke(self, entrada, cfg):
        self.chamadas += 1
        await emitir("tool_result", 1, "1:t", cfg["configurable"]["turn_id"],
                     tool="criar_lembrete", ok=True)
        raise TypeError("'NoneType' object is not iterable")


async def test_efeito_vale_mesmo_com_analytics_fora_do_ar(monkeypatch):
    class _SessaoQuebrada:
        def __call__(self):
            raise RuntimeError("banco de analytics fora do ar")

    # Só o emitir() do analytics perde o banco; o resto do mundo segue de pé.
    monkeypatch.setattr("mordomo.analytics.Sessao", _SessaoQuebrada())
    grafo = _GrafoFalhaComAnalyticsForaDoAr()
    _, respostas = await processar_entrada(_membro(994), _inbound(), grafo)
    assert grafo.chamadas == 1  # NÃO repetiu: o lembrete não pode duplicar
    assert "Fiz o que você pediu" in respostas[0].texto


async def test_anexo_nao_sai_junto_com_mensagem_de_erro():
    """buscar_documento anexou, o grafo caiu depois: o "Ops, tropecei" não
    pode chegar acompanhado do RG de alguém."""
    from mordomo.channels.contract import Anexo
    from mordomo.core.anexos import registrar

    class _GrafoAnexaEDepoisCai:
        async def ainvoke(self, entrada, cfg):
            registrar(cfg, Anexo(documento_id=42, nome="RG do Davi", mime="image/jpeg"))
            await emitir("tool_result", 1, "1:t", cfg["configurable"]["turn_id"],
                         tool="criar_lembrete", ok=True)  # trava o retry
            raise TypeError("crash depois do anexo")

    _, respostas = await processar_entrada(_membro(995), _inbound(), _GrafoAnexaEDepoisCai())
    assert respostas[0].anexos == []


async def test_retry_nao_duplica_anexo():
    """1ª tentativa anexa e cai (sem efeito mutante → repete); a 2ª anexa de
    novo e responde: o documento tem que ir UMA vez."""
    from mordomo.channels.contract import Anexo
    from mordomo.core.anexos import registrar

    class _GrafoAnexaCaiEAnexaDeNovo:
        def __init__(self):
            self.chamadas = 0

        async def ainvoke(self, entrada, cfg):
            self.chamadas += 1
            registrar(cfg, Anexo(documento_id=7, nome="carteirinha", mime="image/jpeg"))
            if self.chamadas == 1:
                raise TypeError("crash na 1ª tentativa")
            return {"messages": [AIMessage("Segue a carteirinha!")]}

    _, respostas = await processar_entrada(_membro(996), _inbound(), _GrafoAnexaCaiEAnexaDeNovo())
    assert len(respostas[0].anexos) == 1
    assert respostas[0].anexos[0].documento_id == 7


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


async def test_timeout_llm_usa_retry_seguro_sem_cancelar_tool():
    class _GrafoTimeoutDepoisTool:
        def __init__(self):
            self.chamadas = 0
            self.tool_concluiu = False

        async def ainvoke(self, entrada, cfg):
            self.chamadas += 1
            if self.chamadas == 1:
                raise LLMDeadlineExceeded(0.05)
            # Mais longa que o prazo da chamada que falhou: seria cancelada pelo
            # desenho antigo, que punha o grafo inteiro sob asyncio.timeout.
            await asyncio.sleep(0.08)
            self.tool_concluiu = True
            return {"messages": [AIMessage("tool terminou")]}

    grafo = _GrafoTimeoutDepoisTool()
    desde = datetime.now(UTC)
    turn_id, respostas = await processar_entrada(_membro(998), _inbound(), grafo)

    eventos = [e for e in await _eventos(desde, ("error",)) if e.turn_id == turn_id]
    assert grafo.chamadas == 2
    assert grafo.tool_concluiu is True
    assert respostas[0].texto == "tool terminou"
    assert len(eventos) == 1
    assert eventos[0].payload["motivo"] == "timeout_llm"


async def test_timeout_llm_depois_de_tool_mutante_nao_repete():
    class _GrafoToolDepoisTimeout:
        def __init__(self):
            self.chamadas = 0

        async def ainvoke(self, entrada, cfg):
            self.chamadas += 1
            await emitir(
                "tool_result", 1, "1:t", cfg["configurable"]["turn_id"],
                tool="criar_lembrete", ok=True,
            )
            raise LLMDeadlineExceeded(0.02)  # segunda chamada LLM, após a tool

    grafo = _GrafoToolDepoisTimeout()
    desde = datetime.now(UTC)
    turn_id, respostas = await processar_entrada(_membro(1001), _inbound(), grafo)

    eventos = [e for e in await _eventos(desde, ("error",)) if e.turn_id == turn_id]
    assert grafo.chamadas == 1
    assert "Fiz o que você pediu" in respostas[0].texto
    assert len(eventos) == 1
    assert eventos[0].payload["motivo"] == "timeout_llm"
    assert eventos[0].payload["efeito"] is True


async def test_dois_timeouts_llm_liberam_fila_para_o_proximo_turno():
    class _GrafoDoisTimeouts:
        def __init__(self):
            self.chamadas = 0
            self.primeira_entrou = asyncio.Event()

        async def ainvoke(self, entrada, cfg):
            self.chamadas += 1
            if self.chamadas <= 2:
                self.primeira_entrou.set()
                await asyncio.sleep(0.02)
                raise LLMDeadlineExceeded(0.02)
            return {"messages": [AIMessage("fila liberada")]}

    grafo = _GrafoDoisTimeouts()
    membro = _membro(1000)
    primeiro = asyncio.create_task(processar_entrada(membro, _inbound(), grafo))
    await grafo.primeira_entrou.wait()
    segundo = asyncio.create_task(processar_entrada(membro, _inbound(), grafo))

    (_, respostas_1), (_, respostas_2) = await asyncio.wait_for(
        asyncio.gather(primeiro, segundo), timeout=0.20
    )

    assert grafo.chamadas == 3
    assert "tropecei" in respostas_1[0].texto
    assert respostas_2[0].texto == "fila liberada"
