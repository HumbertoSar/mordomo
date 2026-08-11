"""ADR-008: detecção de menção e a troca de thread/sessão em grupo."""

from mordomo.channels.contract import InboundMessage
from mordomo.channels.telegram import _direcionado_ao_bot
from mordomo.observability import config_invocacao, session_de_grupo


def test_mencao_ativa_e_e_removida_do_texto():
    ok, texto = _direcionado_ao_bot(
        "@Alfred_Sardenberg_bot o que temos sábado?", "alfred_sardenberg_bot", False
    )
    assert ok and texto == "o que temos sábado?"


def test_sem_mencao_e_sem_reply_fica_quieto():
    ok, _ = _direcionado_ao_bot("gente, que horas é o almoço?", "alfred_sardenberg_bot", False)
    assert not ok


def test_reply_ao_bot_ativa_sem_mencao():
    ok, texto = _direcionado_ao_bot("e no domingo?", "alfred_sardenberg_bot", True)
    assert ok and texto == "e no domingo?"


def test_username_parecido_nao_ativa():
    """@alfred_sardenberg_bot2 é OUTRO bot — o \\b do regex protege."""
    ok, _ = _direcionado_ao_bot("@alfred_sardenberg_bot2 oi", "alfred_sardenberg_bot", False)
    assert not ok


def test_config_de_grupo_troca_thread_e_sessao():
    cfg = config_invocacao(4, "Davi", "adulto", "t-g1", grupo_id="777")
    c = cfg["configurable"]
    assert c["thread_id"] == "grupo-777"
    assert c["session_id"] == session_de_grupo("777")
    assert c["member_id"] == 4  # a identidade continua individual (ADR-003)
    assert c["grupo_id"] == "777"  # nós leem daqui que a conversa é coletiva
    assert cfg["metadata"]["langfuse_session_id"] == c["session_id"]


async def test_cofre_recusa_grupo_antes_do_llm():
    """Segurança, não prompt: em grupo o no_cofre devolve recusa SEM invocar o
    agente (senão o valor/documento sairia para todos no chat)."""
    from sqlalchemy import select

    from mordomo.agents import cofre as agente_cofre
    from mordomo.db.models import ProductEvent
    from mordomo.db.session import Sessao

    # Sentinela: se o guard falhar e o nó tentar criar/invocar o agente real,
    # o teste quebra aqui — não em rede (regra nº 7).
    original = agente_cofre._no_cofre_base.agente

    class _Explode:
        async def ainvoke(self, *a, **k):
            raise AssertionError("cofre invocou o LLM numa conversa de grupo")

    agente_cofre._no_cofre_base.agente = _Explode()
    try:
        cfg = {"configurable": {"member_id": 4, "session_id": "g777:t",
                                "turn_id": "t-cofre-grupo", "grupo_id": "777"}}
        resultado = await agente_cofre.no_cofre({"messages": []}, cfg)
        assert "privado" in resultado["messages"][-1].content
    finally:
        agente_cofre._no_cofre_base.agente = original

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(ProductEvent.turn_id == "t-cofre-grupo")
        )
        tipos = [e.tipo for e in res.scalars()]
    assert "cofre_recusado_grupo" in tipos


async def test_cofre_atende_normalmente_no_privado():
    """Sem grupo_id o guard não pode disparar — o nó segue para o agente."""
    from mordomo.agents import cofre as agente_cofre

    class _AgenteFake:
        async def ainvoke(self, entrada, config):
            from langchain_core.messages import AIMessage

            return {"messages": [*entrada["messages"], AIMessage("do cofre: 04538-132")]}

    original = agente_cofre._no_cofre_base.agente
    agente_cofre._no_cofre_base.agente = _AgenteFake()
    try:
        from langchain_core.messages import HumanMessage

        cfg = {"configurable": {"member_id": 4, "session_id": "4:t", "turn_id": "t-cofre-priv"}}
        resultado = await agente_cofre.no_cofre(
            {"messages": [HumanMessage("qual o CEP de casa?")]}, cfg
        )
        assert "04538-132" in resultado["messages"][-1].content
    finally:
        agente_cofre._no_cofre_base.agente = original


def test_config_privada_continua_por_membro():
    cfg = config_invocacao(4, "Davi", "adulto", "t-g2")
    assert cfg["configurable"]["thread_id"] == "membro-4"


def test_inbound_sem_grupo_por_padrao():
    from datetime import UTC, datetime

    m = InboundMessage(member_id=1, canal="telegram", texto="oi", message_id="1",
                       timestamp=datetime.now(UTC))
    assert m.grupo_id is None
