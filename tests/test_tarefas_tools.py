"""Subagente de Tarefas: domínio, permissões e jornadas reais."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apoio import cfg_de, criar_membro
from mordomo.db.models import Base, ProductEvent, Task
from mordomo.db.session import Sessao


def test_tools_mutantes_de_tarefa_estao_protegidas_de_retry():
    from mordomo.core.efeitos import TOOLS_MUTANTES

    assert {
        "criar_tarefa",
        "concluir_tarefa",
        "cancelar_tarefa",
        "reabrir_tarefa",
    } <= TOOLS_MUTANTES


def test_modelo_tarefa_faz_parte_do_schema():
    assert "tasks" in Base.metadata.tables


async def test_criar_tarefa_rejeita_titulo_vazio():
    from mordomo.tools.tarefas import criar_tarefa

    membro = await criar_membro("TarefaSemTitulo")
    resposta = await criar_tarefa.ainvoke(
        {
            "titulo": "   ",
            "responsavel": None,
            "prazo": None,
            "compartilhada": False,
        },
        cfg_de(membro, "task-empty-title"),
    )

    async with Sessao() as s:
        tarefas = list(
            (
                await s.execute(select(Task).where(Task.criado_por == membro.id))
            ).scalars()
        )

    assert "descrição" in resposta.lower()
    assert tarefas == []


async def test_criar_tarefa_rejeita_titulo_maior_que_o_banco():
    from mordomo.tools.tarefas import criar_tarefa

    membro = await criar_membro("TarefaTituloLongo")
    resposta = await criar_tarefa.ainvoke(
        {
            "titulo": "x" * 301,
            "responsavel": None,
            "prazo": None,
            "compartilhada": False,
        },
        cfg_de(membro, "task-long-title"),
    )

    async with Sessao() as s:
        tarefas = list(
            (
                await s.execute(select(Task).where(Task.criado_por == membro.id))
            ).scalars()
        )

    assert "300" in resposta
    assert tarefas == []


async def test_criacao_e_inicio_da_jornada_compartilham_transacao(monkeypatch):
    import mordomo.tools.tarefas as modulo

    membro = await criar_membro("TarefaTransacaoInicio")

    def evento_invalido(*args, **kwargs):
        return ProductEvent(tipo=None, payload={})

    monkeypatch.setattr(modulo, "evento_de", evento_invalido, raising=False)
    with pytest.raises(IntegrityError):
        await modulo.criar_tarefa.ainvoke(
            {
                "titulo": "não persistir pela metade",
                "responsavel": None,
                "prazo": None,
                "compartilhada": False,
            },
            cfg_de(membro, "task-atomic-create"),
        )

    async with Sessao() as s:
        tarefas = list(
            (
                await s.execute(select(Task).where(Task.criado_por == membro.id))
            ).scalars()
        )
    assert tarefas == []


async def test_criar_tarefa_privada_inicia_jornada_e_preserva_titulo():
    from mordomo.tools.tarefas import criar_tarefa

    membro = await criar_membro("TarefaPrivada")
    resposta = await criar_tarefa.ainvoke(
        {
            "titulo": "separar documentos do exame",
            "responsavel": None,
            "prazo": None,
            "compartilhada": False,
        },
        cfg_de(membro, "task-create-private"),
    )

    async with Sessao() as s:
        tarefa = (
            await s.execute(select(Task).where(Task.criado_por == membro.id))
        ).scalar_one()
        eventos = list(
            (
                await s.execute(
                    select(ProductEvent).where(
                        ProductEvent.turn_id == "task-create-private"
                    )
                )
            ).scalars()
        )

    assert "criada" in resposta.lower()
    assert tarefa.titulo == "separar documentos do exame"
    assert tarefa.responsavel_id == membro.id
    assert tarefa.compartilhada is False
    assert tarefa.status == "aberta"
    assert tarefa.journey_id

    por_tipo = {evento.tipo: evento for evento in eventos}
    assert por_tipo["journey_started"].journey_id == tarefa.journey_id
    assert por_tipo["journey_started"].payload == {
        "journey_type": "task",
        "loads": ["mental"],
    }
    assert por_tipo["task_created"].journey_id == tarefa.journey_id
    assert por_tipo["tool_result"].journey_id == tarefa.journey_id
    assert all("titulo" not in evento.payload for evento in eventos)


async def test_atribuir_a_outro_membro_forca_compartilhamento_e_carga_logistica():
    from mordomo.tools.tarefas import criar_tarefa

    autor = await criar_membro("AutorTarefa")
    responsavel = await criar_membro("DaviTarefa")
    await criar_tarefa.ainvoke(
        {
            "titulo": "buscar os coletores no laboratório",
            "responsavel": "DaviTarefa",
            "prazo": None,
            # Mesmo que o LLM erre este booleano, atribuir a outra pessoa não
            # pode criar uma tarefa invisível para quem precisa executá-la.
            "compartilhada": False,
        },
        cfg_de(autor, "task-create-assigned"),
    )

    async with Sessao() as s:
        tarefa = (
            await s.execute(select(Task).where(Task.criado_por == autor.id))
        ).scalar_one()
        inicio = (
            await s.execute(
                select(ProductEvent).where(
                    ProductEvent.turn_id == "task-create-assigned",
                    ProductEvent.tipo == "journey_started",
                )
            )
        ).scalar_one()

    assert tarefa.responsavel_id == responsavel.id
    assert tarefa.compartilhada is True
    assert inicio.payload["loads"] == ["mental", "logistics"]


async def test_criar_tarefa_com_prazo_resolve_data_deterministicamente():
    from mordomo.tools.tarefas import criar_tarefa

    membro = await criar_membro("TarefaComPrazo")
    resposta = await criar_tarefa.ainvoke(
        {
            "titulo": "confirmar o laboratório",
            "responsavel": None,
            "prazo": "amanhã às 10h",
            "compartilhada": False,
        },
        cfg_de(membro, "task-create-deadline"),
    )

    async with Sessao() as s:
        tarefa = (
            await s.execute(select(Task).where(Task.criado_por == membro.id))
        ).scalar_one()
        criado = (
            await s.execute(
                select(ProductEvent).where(
                    ProductEvent.turn_id == "task-create-deadline",
                    ProductEvent.tipo == "task_created",
                )
            )
        ).scalar_one()

    assert tarefa.prazo_utc is not None
    assert criado.payload["tem_prazo"] is True
    assert "prazo" in resposta.lower()


async def test_listar_isola_privadas_e_mostra_compartilhadas():
    from mordomo.tools.tarefas import listar_tarefas

    leitor = await criar_membro("LeitorTarefas")
    outro = await criar_membro("OutroTarefas")
    async with Sessao() as s:
        s.add_all(
            [
                Task(
                    titulo="minha tarefa privada",
                    criado_por=leitor.id,
                    responsavel_id=leitor.id,
                    compartilhada=False,
                    journey_id="journey-list-own",
                ),
                Task(
                    titulo="segredo do outro",
                    criado_por=outro.id,
                    responsavel_id=outro.id,
                    compartilhada=False,
                    journey_id="journey-list-secret",
                ),
                Task(
                    titulo="tarefa da família",
                    criado_por=outro.id,
                    responsavel_id=None,
                    compartilhada=True,
                    journey_id="journey-list-shared",
                ),
            ]
        )
        await s.commit()

    resposta = await listar_tarefas.ainvoke(
        {"incluir_encerradas": False}, cfg_de(leitor, "task-list-visible")
    )

    assert "minha tarefa privada" in resposta
    assert "tarefa da família" in resposta
    assert "segredo do outro" not in resposta


async def test_listar_mostra_responsavel_deterministico():
    from mordomo.tools.tarefas import listar_tarefas

    autor = await criar_membro("AutorListaResponsavel")
    responsavel = await criar_membro("ResponsavelListaTarefa")
    async with Sessao() as s:
        s.add(
            Task(
                titulo="buscar encomenda",
                criado_por=autor.id,
                responsavel_id=responsavel.id,
                compartilhada=True,
                journey_id="journey-list-owner",
            )
        )
        await s.commit()

    resposta = await listar_tarefas.ainvoke(
        {"incluir_encerradas": False}, cfg_de(autor, "task-list-owner")
    )

    assert "ResponsavelListaTarefa" in resposta


async def test_concluir_tarefa_resolve_a_mesma_jornada():
    from mordomo.tools.tarefas import concluir_tarefa

    membro = await criar_membro("ConcluiTarefa")
    async with Sessao() as s:
        tarefa = Task(
            titulo="confirmar consulta",
            criado_por=membro.id,
            responsavel_id=membro.id,
            compartilhada=False,
            journey_id="journey-task-complete",
        )
        s.add(tarefa)
        await s.commit()
        await s.refresh(tarefa)
        tarefa_id = tarefa.id

    resposta = await concluir_tarefa.ainvoke(
        {"id_tarefa": tarefa_id}, cfg_de(membro, "task-complete")
    )

    async with Sessao() as s:
        tarefa = await s.get(Task, tarefa_id)
        eventos = list(
            (
                await s.execute(
                    select(ProductEvent).where(
                        ProductEvent.turn_id == "task-complete"
                    )
                )
            ).scalars()
        )

    assert tarefa is not None and tarefa.status == "concluida"
    assert "concluída" in resposta.lower()
    por_tipo = {evento.tipo: evento for evento in eventos}
    assert por_tipo["journey_resolved"].journey_id == tarefa.journey_id
    assert por_tipo["task_completed"].journey_id == tarefa.journey_id
    assert por_tipo["tool_result"].journey_id == tarefa.journey_id


async def test_transicao_e_evento_de_jornada_compartilham_transacao(monkeypatch):
    import mordomo.tools.tarefas as modulo

    membro = await criar_membro("TarefaTransacaoConclusao")
    async with Sessao() as s:
        tarefa = Task(
            titulo="continuar aberta se o fato falhar",
            criado_por=membro.id,
            responsavel_id=membro.id,
            compartilhada=False,
            journey_id="journey-atomic-transition",
        )
        s.add(tarefa)
        await s.commit()
        await s.refresh(tarefa)
        tarefa_id = tarefa.id

    def evento_invalido(*args, **kwargs):
        return ProductEvent(tipo=None, payload={})

    monkeypatch.setattr(modulo, "evento_de", evento_invalido)
    with pytest.raises(IntegrityError):
        await modulo.concluir_tarefa.ainvoke(
            {"id_tarefa": tarefa_id}, cfg_de(membro, "task-atomic-complete")
        )

    async with Sessao() as s:
        tarefa = await s.get(Task, tarefa_id)
    assert tarefa is not None and tarefa.status == "aberta"


async def test_cancelar_tarefa_abandona_em_vez_de_resolver():
    from mordomo.tools.tarefas import cancelar_tarefa

    membro = await criar_membro("CancelaTarefa")
    async with Sessao() as s:
        tarefa = Task(
            titulo="pesquisar item que não precisamos mais",
            criado_por=membro.id,
            responsavel_id=membro.id,
            compartilhada=False,
            journey_id="journey-task-cancel",
        )
        s.add(tarefa)
        await s.commit()
        await s.refresh(tarefa)
        tarefa_id = tarefa.id

    await cancelar_tarefa.ainvoke(
        {"id_tarefa": tarefa_id}, cfg_de(membro, "task-cancel")
    )

    async with Sessao() as s:
        tarefa = await s.get(Task, tarefa_id)
        abandono = (
            await s.execute(
                select(ProductEvent).where(
                    ProductEvent.turn_id == "task-cancel",
                    ProductEvent.tipo == "journey_abandoned",
                )
            )
        ).scalar_one()

    assert tarefa is not None and tarefa.status == "cancelada"
    assert abandono.journey_id == tarefa.journey_id
    assert abandono.payload["reason"] == "user_cancelled"


async def test_reabrir_tarefa_reabre_a_jornada():
    from mordomo.tools.tarefas import reabrir_tarefa

    membro = await criar_membro("ReabreTarefa")
    async with Sessao() as s:
        tarefa = Task(
            titulo="comprar o item que faltou",
            criado_por=membro.id,
            responsavel_id=membro.id,
            compartilhada=False,
            status="concluida",
            journey_id="journey-task-reopen",
        )
        s.add(tarefa)
        await s.commit()
        await s.refresh(tarefa)
        tarefa_id = tarefa.id

    await reabrir_tarefa.ainvoke(
        {"id_tarefa": tarefa_id}, cfg_de(membro, "task-reopen")
    )

    async with Sessao() as s:
        tarefa = await s.get(Task, tarefa_id)
        reabertura = (
            await s.execute(
                select(ProductEvent).where(
                    ProductEvent.turn_id == "task-reopen",
                    ProductEvent.tipo == "journey_reopened",
                )
            )
        ).scalar_one()

    assert tarefa is not None and tarefa.status == "aberta"
    assert reabertura.journey_id == tarefa.journey_id
    assert reabertura.payload["reason"] == "user_reopened"


async def test_mutacao_bloqueia_privada_de_outro_e_permite_compartilhada():
    from mordomo.tools.tarefas import concluir_tarefa

    dono = await criar_membro("DonoPrivadaTarefa")
    outro = await criar_membro("OutroMutacaoTarefa")
    async with Sessao() as s:
        privada = Task(
            titulo="privada",
            criado_por=dono.id,
            responsavel_id=dono.id,
            compartilhada=False,
            journey_id="journey-private-permission",
        )
        compartilhada = Task(
            titulo="compartilhada",
            criado_por=dono.id,
            responsavel_id=None,
            compartilhada=True,
            journey_id="journey-shared-permission",
        )
        s.add_all([privada, compartilhada])
        await s.commit()
        await s.refresh(privada)
        await s.refresh(compartilhada)
        privada_id, compartilhada_id = privada.id, compartilhada.id

    negada = await concluir_tarefa.ainvoke(
        {"id_tarefa": privada_id}, cfg_de(outro, "task-private-denied")
    )
    permitida = await concluir_tarefa.ainvoke(
        {"id_tarefa": compartilhada_id}, cfg_de(outro, "task-shared-allowed")
    )

    async with Sessao() as s:
        privada = await s.get(Task, privada_id)
        compartilhada = await s.get(Task, compartilhada_id)

    assert "não encontrada" in negada.lower()
    assert privada is not None and privada.status == "aberta"
    assert "concluída" in permitida.lower()
    assert compartilhada is not None and compartilhada.status == "concluida"


async def test_duas_conclusoes_simultaneas_resolvem_jornada_uma_vez(monkeypatch):
    import mordomo.tools.tarefas as modulo

    concluir_tarefa = modulo.concluir_tarefa

    autor = await criar_membro("AutorCorridaTarefa")
    outro = await criar_membro("OutroCorridaTarefa")
    async with Sessao() as s:
        tarefa = Task(
            titulo="não duplicar resolução",
            criado_por=autor.id,
            compartilhada=True,
            journey_id="journey-task-race",
        )
        s.add(tarefa)
        await s.commit()
        await s.refresh(tarefa)
        tarefa_id = tarefa.id

    sessao_real = modulo.Sessao
    barreira = asyncio.Barrier(2)

    class SessaoComBarreira:
        def __init__(self):
            self._contexto = sessao_real()

        async def __aenter__(self):
            self._sessao = await self._contexto.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self._contexto.__aexit__(*args)

        async def get(self, *args, **kwargs):
            tarefa_lida = await self._sessao.get(*args, **kwargs)
            await barreira.wait()
            return tarefa_lida

        def __getattr__(self, nome):
            return getattr(self._sessao, nome)

    monkeypatch.setattr(modulo, "Sessao", SessaoComBarreira)

    await asyncio.gather(
        concluir_tarefa.ainvoke(
            {"id_tarefa": tarefa_id}, cfg_de(autor, "task-race-author")
        ),
        concluir_tarefa.ainvoke(
            {"id_tarefa": tarefa_id}, cfg_de(outro, "task-race-other")
        ),
    )

    async with Sessao() as s:
        eventos = list(
            (
                await s.execute(
                    select(ProductEvent).where(
                        ProductEvent.journey_id == "journey-task-race"
                    )
                )
            ).scalars()
        )
    assert sum(e.tipo == "journey_resolved" for e in eventos) == 1
