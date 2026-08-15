"""Analytics de jornadas: uma necessidade pode atravessar vários turnos."""

from datetime import UTC, datetime

from sqlalchemy import select

from mordomo.analytics import emitir, emitir_de
from mordomo.db.models import ProductEvent
from mordomo.db.session import Sessao


async def test_emissor_persiste_journey_id_fora_do_payload():
    await emitir(
        "journey_started",
        member_id=1,
        session_id="1:hoje",
        turn_id="turn-jornada-1",
        journey_id="journey-1",
        journey_type="task",
        loads=["mental", "logistics"],
    )

    async with Sessao() as s:
        evento = (
            await s.execute(
                select(ProductEvent)
                .where(ProductEvent.turn_id == "turn-jornada-1")
            )
        ).scalar_one()

    assert evento.journey_id == "journey-1"
    assert "journey_id" not in evento.payload
    assert evento.payload["journey_type"] == "task"


async def test_emissor_de_propaga_journey_id_do_contexto():
    config = {
        "configurable": {
            "member_id": 1,
            "session_id": "1:hoje",
            "turn_id": "turn-jornada-config",
            "journey_id": "journey-config",
        }
    }

    await emitir_de(config, "journey_resolved")

    async with Sessao() as s:
        evento = (
            await s.execute(
                select(ProductEvent)
                .where(ProductEvent.turn_id == "turn-jornada-config")
            )
        ).scalar_one()
    assert evento.journey_id == "journey-config"


async def test_emissor_de_aceita_jornada_explicita_para_tool_que_a_criou():
    config = {
        "configurable": {
            "member_id": 1,
            "session_id": "1:hoje",
            "turn_id": "turn-jornada-explicita",
        }
    }

    await emitir_de(
        config,
        "tool_result",
        journey_id="journey-explicita",
        tool="criar_tarefa",
        ok=True,
    )

    async with Sessao() as s:
        evento = (
            await s.execute(
                select(ProductEvent)
                .where(ProductEvent.turn_id == "turn-jornada-explicita")
            )
        ).scalar_one()
    assert evento.journey_id == "journey-explicita"
    assert "journey_id" not in evento.payload


async def test_jornadas_agregam_desfecho_final_sem_confundir_reabertura():
    from mordomo.reporting.queries import jornadas

    # O banco de teste é compartilhado pela sessão; a janela começa AGORA para
    # não contar jornadas semeadas pelos testes anteriores deste arquivo.
    desde = datetime.now(UTC)

    await emitir(
        "journey_started", 1, "1:hoje", "turn-ja-1",
        journey_id="journey-aberta", journey_type="task", loads=["mental"],
    )
    await emitir(
        "journey_started", 1, "1:hoje", "turn-jr-1",
        journey_id="journey-resolvida", journey_type="shopping",
        loads=["mental", "logistics"],
    )
    await emitir(
        "journey_resolved", 1, "1:hoje", "turn-jr-2",
        journey_id="journey-resolvida",
    )
    await emitir(
        "journey_started", 2, "2:hoje", "turn-jab-1",
        journey_id="journey-abandonada", journey_type="research", loads=["creative"],
    )
    await emitir(
        "journey_abandoned", 2, "2:hoje", "turn-jab-2",
        journey_id="journey-abandonada", reason="sem_interesse",
    )
    await emitir(
        "journey_started", 2, "2:hoje", "turn-jre-1",
        journey_id="journey-reaberta", journey_type="task", loads=["mental"],
    )
    await emitir(
        "journey_resolved", 2, "2:hoje", "turn-jre-2",
        journey_id="journey-reaberta",
    )
    await emitir(
        "journey_reopened", 2, "2:hoje", "turn-jre-3",
        journey_id="journey-reaberta", reason="faltou_etapa",
    )

    resultado = await jornadas(desde)

    assert resultado["iniciadas"] == 4
    assert resultado["resolvidas"] == 1
    assert resultado["abandonadas"] == 1
    assert resultado["abertas"] == 2
    assert resultado["reaberturas"] == 1
    assert resultado["taxa_resolucao"] == 0.5
    assert resultado["por_tipo"] == {"research": 1, "shopping": 1, "task": 2}
    assert resultado["por_carga"] == {"creative": 1, "logistics": 1, "mental": 3}
    assert resultado["tempo_resolucao_p50_s"] is not None


async def test_coletor_expoe_jornadas_sem_inventar_taxa_sem_base():
    from mordomo.reporting.queries import coletar

    resultado = await coletar(dias=0)

    assert resultado["jornadas"]["iniciadas"] == 0
    assert resultado["jornadas"]["taxa_resolucao"] is None
