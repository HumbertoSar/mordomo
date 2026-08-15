"""O grafo inteiro sem rede: supervisor → (responder | subagente) → resposta.

Até aqui nenhum teste tocava supervisor, grafo ou pipeline — qualquer mudança
de prompt ou de fiação só quebrava EM PRODUÇÃO. Estes testes usam um chat model
falso e determinístico no lugar do OpenRouter (regra nº 7: sem rede, sem
chaves); o roteamento REAL do LLM continua sendo medido pelo eval nº 2.

O fake imita o contrato exato de `with_structured_output(Decisao,
include_raw=True)`: devolve {"raw", "parsed", "parsing_error"} — inclusive o
caminho de falha de parse, que nenhum eval cobre (o modelo real raramente
falha de propósito)."""

from langchain_core.messages import AIMessage, HumanMessage

from mordomo.agents.supervisor import PROMPT_SUPERVISOR, Decisao
from mordomo.core.graph import build_graph


def _cfg(member_id: int = 900, turn_id: str = "turno-teste") -> dict:
    """Config mínima com o mesmo formato do config_invocacao de produção."""
    return {
        "configurable": {
            "thread_id": f"membro-{member_id}",
            "member_id": member_id,
            "member_nome": "Teste",
            "member_papel": "adulto",
            "session_id": f"{member_id}:teste",
            "turn_id": turn_id,
        }
    }


class _ModeloFalso:
    """Substitui o ChatOpenAI do supervisor. `decisor(texto) -> Decisao | None`;
    None simula o modelo falhando em produzir o JSON do structured output."""

    def __init__(self, decisor):
        self._decisor = decisor

    def with_structured_output(self, schema, include_raw=False):
        return self

    async def ainvoke(self, mensagens, config=None):
        decisao = self._decisor(mensagens[-1].content)
        bruto = AIMessage(
            "",
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        )
        return {
            "raw": bruto,
            "parsed": decisao,
            "parsing_error": None if decisao else ValueError("json inválido"),
        }


class _AgenteFalso:
    """Substitui o subagente ReAct: devolve as mensagens de entrada + resposta."""

    def __init__(self, resposta: str):
        self._resposta = resposta

    async def ainvoke(self, entrada, config=None):
        final = AIMessage(
            self._resposta,
            usage_metadata={"input_tokens": 500, "output_tokens": 30, "total_tokens": 530},
        )
        return {"messages": [*entrada["messages"], final]}


def _roteador(texto: str) -> Decisao | None:
    if "tarefa" in texto:
        return Decisao(destino="tarefas")
    if "lembra" in texto:
        return Decisao(destino="lembretes")
    if "quebra-parse" in texto:
        return None
    return Decisao(destino="responder", resposta="Às ordens!")


def test_supervisor_aceita_destino_tarefas():
    assert _roteador("cria uma tarefa").destino == "tarefas"


def test_capacidades_atuais_vencem_afirmacoes_antigas_do_historico():
    """Regressão real: após o deploy, recusas antigas fizeram o supervisor
    negar a capacidade de tarefas que já estava descrita no próprio prompt."""
    prompt = PROMPT_SUPERVISOR.lower()
    assert "fonte de verdade" in prompt
    assert "afirmações antigas" in prompt
    assert "tarefas é uma capacidade oficial e já está em produção" in prompt
    assert "texto pronto para copiar" in prompt
    assert "novidade no mordomo: agora você pode criar" in prompt


async def test_caminho_responder_termina_no_supervisor(monkeypatch):
    monkeypatch.setattr(
        "mordomo.agents.supervisor.chat_model", lambda *a, **k: _ModeloFalso(_roteador)
    )
    grafo = build_graph()
    resultado = await grafo.ainvoke({"messages": [HumanMessage("bom dia!")]}, _cfg())
    assert resultado["messages"][-1].content == "Às ordens!"


async def test_caminho_subagente_flui_ate_a_resposta(monkeypatch):
    monkeypatch.setattr(
        "mordomo.agents.supervisor.chat_model", lambda *a, **k: _ModeloFalso(_roteador)
    )
    monkeypatch.setattr(
        "mordomo.agents.lembretes.no_lembretes.agente", _AgenteFalso("Lembrete #7 criado!")
    )
    grafo = build_graph()
    resultado = await grafo.ainvoke(
        {"messages": [HumanMessage("me lembra de pagar o boleto")]}, _cfg(turn_id="t-sub")
    )
    assert resultado["messages"][-1].content == "Lembrete #7 criado!"


async def test_caminho_tarefas_flui_ate_a_resposta(monkeypatch):
    monkeypatch.setattr(
        "mordomo.agents.supervisor.chat_model", lambda *a, **k: _ModeloFalso(_roteador)
    )
    monkeypatch.setattr(
        "mordomo.agents.tarefas.no_tarefas.agente", _AgenteFalso("Tarefa #4 criada!")
    )
    grafo = build_graph()
    resultado = await grafo.ainvoke(
        {"messages": [HumanMessage("cria uma tarefa para comprar pão")]},
        _cfg(turn_id="t-task-graph"),
    )
    assert resultado["messages"][-1].content == "Tarefa #4 criada!"


async def test_falha_de_parse_nao_derruba_a_conversa(monkeypatch):
    """Modelo fraco que não produz JSON → resposta educada, não exceção."""
    monkeypatch.setattr(
        "mordomo.agents.supervisor.chat_model", lambda *a, **k: _ModeloFalso(_roteador)
    )
    grafo = build_graph()
    resultado = await grafo.ainvoke(
        {"messages": [HumanMessage("quebra-parse")]}, _cfg(turn_id="t-parse")
    )
    assert "repetir" in resultado["messages"][-1].content.lower()


async def test_eventos_do_turno_carregam_contexto(monkeypatch):
    """A regra nº 4 de ponta a ponta: os eventos emitidos pelo grafo saem com
    member/session/turn do configurable — nunca órfãos."""
    from sqlalchemy import select

    from mordomo.db.models import ProductEvent
    from mordomo.db.session import Sessao

    monkeypatch.setattr(
        "mordomo.agents.supervisor.chat_model", lambda *a, **k: _ModeloFalso(_roteador)
    )
    grafo = build_graph()
    await grafo.ainvoke({"messages": [HumanMessage("bom dia!")]}, _cfg(turn_id="t-analytics"))

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(ProductEvent.turn_id == "t-analytics")
        )
        eventos = list(res.scalars())

    tipos = {e.tipo for e in eventos}
    assert "orchestrator_decision" in tipos
    assert "llm_usage" in tipos
    assert all(e.session_id and e.member_id for e in eventos)
