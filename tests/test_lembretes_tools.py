"""tools/lembretes: a orquestração da tool — os ramos onde nascem os
tool_result e motivos que alimentam dashboard, curadoria e evals.

O scheduler é substituído por um registro em memória: teste não sobe
APScheduler nem depende de relógio (regra nº 7)."""

import pytest
from sqlalchemy import select

from apoio import cfg_de, criar_membro
from mordomo.db.models import ProductEvent, Reminder
from mordomo.db.session import Sessao
from mordomo.tools.lembretes import cancelar_lembrete, criar_lembrete, listar_lembretes


@pytest.fixture
def agenda_falsa(monkeypatch):
    """Captura os agendamentos sem subir o APScheduler."""
    agendados: list[tuple[int, object]] = []
    monkeypatch.setattr("mordomo.scheduler.agendar", lambda id, dt: agendados.append((id, dt)))
    monkeypatch.setattr("mordomo.scheduler.cancelar_job", lambda id: None)
    return agendados


async def _motivo(turn: str) -> str | None:
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(
                ProductEvent.turn_id == turn, ProductEvent.tipo == "tool_result"
            )
        )
        eventos = list(res.scalars())
    return eventos[-1].payload.get("motivo") if eventos else None


async def test_criar_unico_agenda_e_emite_ok(agenda_falsa):
    m = await criar_membro("LembreteOk")
    r = await criar_lembrete.ainvoke(
        {"texto": "pagar o boleto", "quando": "amanhã às 8h"}, cfg_de(m, "t-l1")
    )
    assert "criado" in r and "pagar o boleto" in r
    assert len(agenda_falsa) == 1  # foi para o scheduler


async def test_data_nao_entendida_pede_esclarecimento(agenda_falsa):
    m = await criar_membro("LembreteConfuso")
    r = await criar_lembrete.ainvoke(
        {"texto": "x", "quando": "quando der vontade"}, cfg_de(m, "t-l2")
    )
    assert "NÃO ENTENDI" in r
    assert await _motivo("t-l2") == "data_nao_entendida"
    assert agenda_falsa == []  # nada agendado, nada gravado


async def test_data_no_passado_pede_confirmacao(agenda_falsa):
    m = await criar_membro("LembretePassado")
    r = await criar_lembrete.ainvoke(
        {"texto": "x", "quando": "ontem às 10h"}, cfg_de(m, "t-l3")
    )
    assert "já passou" in r
    assert await _motivo("t-l3") == "data_no_passado"
    assert agenda_falsa == []


async def test_recorrencia_sem_hora_pergunta(agenda_falsa):
    m = await criar_membro("LembreteRecSemHora")
    r = await criar_lembrete.ainvoke(
        {"texto": "tomar remédio", "quando": "todo dia"}, cfg_de(m, "t-l4")
    )
    assert "FALTA A HORA" in r
    assert await _motivo("t-l4") == "recorrencia_sem_hora"
    assert agenda_falsa == []


async def test_recorrente_com_hora_cria_e_agenda(agenda_falsa):
    m = await criar_membro("LembreteRec")
    r = await criar_lembrete.ainvoke(
        {"texto": "tomar remédio", "quando": "todo dia às 8h"}, cfg_de(m, "t-l5")
    )
    assert "recorrente" in r.lower()
    assert len(agenda_falsa) == 1
    async with Sessao() as s:
        res = await s.execute(select(Reminder).where(Reminder.member_id == m.id))
        lembrete = res.scalar_one()
    assert lembrete.recorrencia  # a regra foi serializada


async def test_cancelar_de_outro_membro_nao_encontra(agenda_falsa):
    dono = await criar_membro("LembreteDono")
    intruso = await criar_membro("LembreteIntruso")
    await criar_lembrete.ainvoke(
        {"texto": "meu segredo", "quando": "amanhã às 9h"}, cfg_de(dono, "t-l6")
    )
    async with Sessao() as s:
        res = await s.execute(select(Reminder).where(Reminder.member_id == dono.id))
        alvo = res.scalar_one()

    r = await cancelar_lembrete.ainvoke({"id_lembrete": alvo.id}, cfg_de(intruso, "t-l7"))
    assert "não encontrado" in r
    assert await _motivo("t-l7") == "nao_encontrado"

    r = await cancelar_lembrete.ainvoke({"id_lembrete": alvo.id}, cfg_de(dono, "t-l8"))
    assert "cancelado" in r
    # cancelar de novo: já resolvido
    r = await cancelar_lembrete.ainvoke({"id_lembrete": alvo.id}, cfg_de(dono, "t-l9"))
    assert await _motivo("t-l9") == "ja_resolvido"


async def test_listar_mostra_pendentes_do_membro(agenda_falsa):
    m = await criar_membro("LembreteLista")
    await criar_lembrete.ainvoke(
        {"texto": "buscar bolo", "quando": "amanhã às 15h"}, cfg_de(m, "t-l10")
    )
    r = await listar_lembretes.ainvoke({}, cfg_de(m, "t-l11"))
    assert "buscar bolo" in r
