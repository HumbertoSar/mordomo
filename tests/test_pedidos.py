"""Pedidos de funcionalidade: payload da issue, nó determinístico e degradação
sem token (o pedido nunca se perde)."""

from langchain_core.messages import HumanMessage
from sqlalchemy import select

from mordomo.agents.pedidos import no_pedidos
from mordomo.db.models import ProductEvent
from mordomo.db.session import Sessao
from mordomo.github_issues import montar_payload


def test_payload_padrao_nao_publica_conversa_nem_autor():
    """O repo das issues é PÚBLICO: sem detalhado=True, nem o texto original
    nem o nome do autor podem aparecer no corpo (só o título curto)."""
    p = montar_payload("pedir pizza pelo mordomo", "você devia saber pedir pizza", "Davi", "adulto")
    assert p["title"] == "[família] pedir pizza pelo mordomo"
    assert "Davi" not in p["body"]
    assert "pedir pizza" not in p["body"]  # o pedido bruto fica em product_events
    assert p["labels"] == ["pedido-da-familia"]


def test_payload_detalhado_tem_rotulo_e_autor():
    p = montar_payload("pedir pizza pelo mordomo", "você devia saber pedir pizza",
                       "Davi", "adulto", detalhado=True)
    assert "Davi" in p["body"] and "pedir pizza" in p["body"]


def test_payload_detalhado_cita_todas_as_linhas():
    """Pedido multilinha: sem o '> ' por linha, a 2ª linha escapa do blockquote
    e vira markdown solto (título, imagem) publicado em nome do bot."""
    p = montar_payload("t", "linha um\n# injeção de título", "A", "adulto", detalhado=True)
    assert "> linha um" in p["body"]
    assert "> # injeção de título" in p["body"]
    assert "\n# injeção" not in p["body"]


def test_titulo_gigante_e_truncado():
    p = montar_payload("x" * 300, "pedido", "A", "adulto")
    assert len(p["title"]) <= len("[família] ") + 80


def test_problema_ganha_rotulo_e_prefixo_proprios():
    p = montar_payload("erro no lembrete de ontem", "deu erro quando pedi", "Davi", "adulto",
                       categoria="problema")
    assert p["title"].startswith("[problema]")
    assert p["labels"] == ["problema-reportado"]
    assert "Problema relatado" in p["body"]


def test_curadoria_sem_detalhe_aponta_para_o_funil():
    corpo_com_casos = '```json\n{"expressao": "amanhã dpois do almoço"}\n```'
    p = montar_payload("3 caso(s) de eval propostos", corpo_com_casos,
                       "curadoria automática", "sistema", categoria="curadoria")
    assert p["labels"] == ["curadoria-evals"]
    assert "amanhã dpois" not in p["body"]  # expressão real da família não vaza
    assert "product_events" in p["body"]


async def test_no_pedidos_problema_pede_desculpa():
    from mordomo.config import settings

    original = settings.github_token
    settings.github_token = ""
    try:
        estado = {
            "messages": [HumanMessage("deu erro quando pedi o lembrete")],
            "member_nome": "Davi", "member_papel": "adulto",
            "pedido_titulo": "erro ao criar lembrete", "pedido_tipo": "problema",
        }
        cfg = {"configurable": {"member_id": 901, "session_id": "901:t", "turn_id": "t-prob-1"}}
        resultado = await no_pedidos(estado, cfg)
        assert "Sinto muito" in resultado["messages"][-1].content
    finally:
        settings.github_token = original

    async with Sessao() as s:
        res = await s.execute(select(ProductEvent).where(ProductEvent.turn_id == "t-prob-1"))
        payloads = {e.tipo: e.payload for e in res.scalars()}
    assert payloads["feature_requested"]["categoria"] == "problema"


async def test_no_pedidos_sem_token_registra_e_agradece():
    """GITHUB_TOKEN vazio (garantido pelo conftest? não — settings lê .env local;
    o conftest zera OPENROUTER/LANGFUSE mas não GITHUB. Zeramos aqui)."""
    from mordomo.config import settings

    original = settings.github_token
    settings.github_token = ""
    try:
        estado = {
            "messages": [HumanMessage("você devia saber pedir pizza")],
            "member_id": 900,
            "member_nome": "Davi",
            "member_papel": "adulto",
            "pedido_titulo": "pedir pizza pelo mordomo",
        }
        cfg = {"configurable": {"member_id": 900, "session_id": "900:t", "turn_id": "t-pedido-1"}}
        resultado = await no_pedidos(estado, cfg)
        assert "pedir pizza pelo mordomo" in resultado["messages"][-1].content
        assert "Davi" in resultado["messages"][-1].content
    finally:
        settings.github_token = original

    async with Sessao() as s:
        res = await s.execute(select(ProductEvent).where(ProductEvent.turn_id == "t-pedido-1"))
        tipos = {e.tipo: e.payload for e in res.scalars()}
    assert "feature_requested" in tipos
    assert tipos["feature_requested"]["titulo"] == "pedir pizza pelo mordomo"
    assert tipos["feature_issue_created"]["ok"] is False  # sem token → sem issue, MAS registrado
