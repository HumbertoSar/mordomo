"""Tools do Subagente de Tarefas — pendências com desfecho observável."""

import uuid
from datetime import UTC

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import func, or_, select, update

from ..analytics import emitir_de, evento_de
from ..db.models import Member, Task
from ..db.session import Sessao
from ._comum import fmt_data, resolver_ou_instruir


@tool
async def criar_tarefa(
    titulo: str,
    responsavel: str | None,
    prazo: str | None,
    compartilhada: bool,
    config: RunnableConfig,
) -> str:
    """Cria uma tarefa privada ou compartilhada, com responsável e prazo opcionais.

    Args:
        titulo: descrição curta da pendência.
        responsavel: nome de um membro da família, ou null para o próprio autor.
        prazo: expressão de tempo em português, ou null quando não houver prazo.
        compartilhada: true somente quando a família ou outra pessoa deve vê-la.
    """
    member_id = config["configurable"]["member_id"]
    journey_id = uuid.uuid4().hex
    await emitir_de(
        config, "tool_called", journey_id=journey_id, tool="criar_tarefa"
    )
    titulo = titulo.strip()
    if not titulo:
        await emitir_de(
            config,
            "tool_result",
            journey_id=journey_id,
            tool="criar_tarefa",
            ok=False,
            motivo="titulo_vazio",
        )
        return "Qual é a descrição da tarefa?"
    if len(titulo) > 300:
        await emitir_de(
            config,
            "tool_result",
            journey_id=journey_id,
            tool="criar_tarefa",
            ok=False,
            motivo="titulo_longo",
        )
        return "A descrição da tarefa deve ter no máximo 300 caracteres."

    responsavel_id: int | None
    if responsavel:
        async with Sessao() as s:
            encontrado = await s.scalar(
                select(Member).where(func.lower(Member.nome) == responsavel.strip().lower())
            )
        if encontrado is None:
            await emitir_de(
                config,
                "tool_result",
                journey_id=journey_id,
                tool="criar_tarefa",
                ok=False,
                motivo="responsavel_nao_encontrado",
            )
            return f'Não encontrei "{responsavel}" entre os membros da família.'
        responsavel_id = encontrado.id
    else:
        # Privada sem nome = tarefa do próprio autor; compartilhada sem nome =
        # qualquer pessoa da família pode assumir.
        responsavel_id = None if compartilhada else member_id

    atribuida_a_outro = responsavel_id is not None and responsavel_id != member_id
    compartilhada = compartilhada or atribuida_a_outro
    cargas = ["mental", "logistics"] if compartilhada else ["mental"]

    prazo_dt = None
    if prazo:
        config_jornada = {
            **config,
            "configurable": {
                **config.get("configurable", {}),
                "journey_id": journey_id,
            },
        }
        prazo_dt, instrucao = await resolver_ou_instruir(
            prazo, config_jornada, "criar_tarefa"
        )
        if instrucao:
            return instrucao

    async with Sessao() as s:
        tarefa = Task(
            titulo=titulo,
            criado_por=member_id,
            responsavel_id=responsavel_id,
            compartilhada=compartilhada,
            prazo_utc=prazo_dt.astimezone(UTC) if prazo_dt else None,
            journey_id=journey_id,
        )
        s.add(tarefa)
        await s.flush()
        s.add_all(
            [
                evento_de(
                    config,
                    "journey_started",
                    journey_id=journey_id,
                    journey_type="task",
                    loads=cargas,
                ),
                evento_de(
                    config,
                    "task_created",
                    journey_id=journey_id,
                    task_id=tarefa.id,
                    compartilhada=compartilhada,
                    responsavel_id=responsavel_id,
                    tem_prazo=prazo_dt is not None,
                ),
            ]
        )
        await s.commit()
        tarefa_id = tarefa.id

    await emitir_de(
        config,
        "tool_result",
        journey_id=journey_id,
        tool="criar_tarefa",
        ok=True,
        task_id=tarefa_id,
    )
    sufixo = f" · prazo {fmt_data(prazo_dt)}" if prazo_dt else ""
    return f"Tarefa #{tarefa_id} criada: {titulo}{sufixo}"


@tool
async def listar_tarefas(
    incluir_encerradas: bool, config: RunnableConfig
) -> str:
    """Lista tarefas visíveis; por padrão somente as abertas.

    Args:
        incluir_encerradas: true quando o usuário pedir concluídas/canceladas.
    """
    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="listar_tarefas")
    filtros = [
        or_(Task.compartilhada.is_(True), Task.criado_por == member_id),
    ]
    if not incluir_encerradas:
        filtros.append(Task.status == "aberta")
    async with Sessao() as s:
        resultado = await s.execute(
            select(Task).where(*filtros).order_by(Task.criado_em, Task.id)
        )
        tarefas = list(resultado.scalars())
        ids_responsaveis = {
            tarefa.responsavel_id
            for tarefa in tarefas
            if tarefa.responsavel_id is not None
        }
        if ids_responsaveis:
            nomes = (
                await s.execute(
                    select(Member.id, Member.nome).where(
                        Member.id.in_(ids_responsaveis)
                    )
                )
            ).all()
            responsaveis = {id_membro: nome for id_membro, nome in nomes}
        else:
            responsaveis = {}

    await emitir_de(
        config,
        "tool_result",
        tool="listar_tarefas",
        ok=True,
        n=len(tarefas),
        incluir_encerradas=incluir_encerradas,
    )
    if not tarefas:
        return "Nenhuma tarefa encontrada."

    def linha(tarefa: Task) -> str:
        escopo = "família" if tarefa.compartilhada else "privada"
        prazo = f" · prazo {fmt_data(tarefa.prazo_utc)}" if tarefa.prazo_utc else ""
        responsavel = (
            f" · responsável {responsaveis[tarefa.responsavel_id]}"
            if tarefa.responsavel_id in responsaveis
            else " · sem responsável"
        )
        return (
            f"#{tarefa.id} [{tarefa.status}; {escopo}] "
            f"{tarefa.titulo}{responsavel}{prazo}"
        )

    return "\n".join(linha(tarefa) for tarefa in tarefas)


async def _mudar_status(
    id_tarefa: int,
    config: RunnableConfig,
    *,
    tool_name: str,
    estados_validos: frozenset[str],
    novo_status: str,
    evento_jornada: str,
    evento_tarefa: str,
    rotulo: str,
    reason: str | None = None,
) -> str:
    """Transição compartilhada pelas três tools, com privacidade e Analytics."""
    member_id = config["configurable"]["member_id"]
    await emitir_de(
        config, "tool_called", tool=tool_name, task_id=id_tarefa
    )
    visibilidade = or_(
        Task.compartilhada.is_(True), Task.criado_por == member_id
    )
    async with Sessao() as s:
        resultado = await s.execute(
            update(Task)
            .where(
                Task.id == id_tarefa,
                visibilidade,
                Task.status.in_(estados_validos),
            )
            .values(status=novo_status)
            .returning(Task.journey_id, Task.titulo)
        )
        alterada = resultado.first()
        if alterada is not None:
            journey_id, titulo = alterada
            payload_jornada = {"reason": reason} if reason else {}
            s.add_all(
                [
                    evento_de(
                        config,
                        evento_jornada,
                        journey_id=journey_id,
                        **payload_jornada,
                    ),
                    evento_de(
                        config,
                        evento_tarefa,
                        journey_id=journey_id,
                        task_id=id_tarefa,
                    ),
                ]
            )
            await s.commit()
            estado_atual = None
        else:
            atual = (
                await s.execute(
                    select(Task.status, Task.journey_id).where(
                        Task.id == id_tarefa, visibilidade
                    )
                )
            ).first()
            await s.rollback()
            estado_atual = atual

    if alterada is None:
        if estado_atual is None:
            await emitir_de(
                config,
                "tool_result",
                tool=tool_name,
                ok=False,
                motivo="nao_encontrado",
            )
            return f"Tarefa #{id_tarefa} não encontrada entre as tarefas visíveis."
        estado, journey_id = estado_atual
        await emitir_de(
            config,
            "tool_result",
            journey_id=journey_id,
            tool=tool_name,
            ok=False,
            motivo="estado_invalido",
            estado=estado,
        )
        return f"Tarefa #{id_tarefa} já está {estado}."

    # O ramo sem alteração sempre retorna acima; daqui em diante a linha existe.
    assert alterada is not None
    journey_id, titulo = alterada
    await emitir_de(
        config,
        "tool_result",
        journey_id=journey_id,
        tool=tool_name,
        ok=True,
        task_id=id_tarefa,
    )
    return f"Tarefa #{id_tarefa} {rotulo}: {titulo}"


@tool
async def concluir_tarefa(id_tarefa: int, config: RunnableConfig) -> str:
    """Marca como concluída uma tarefa visível pelo número."""
    return await _mudar_status(
        id_tarefa,
        config,
        tool_name="concluir_tarefa",
        estados_validos=frozenset({"aberta"}),
        novo_status="concluida",
        evento_jornada="journey_resolved",
        evento_tarefa="task_completed",
        rotulo="concluída",
    )


@tool
async def cancelar_tarefa(id_tarefa: int, config: RunnableConfig) -> str:
    """Cancela uma tarefa aberta; cancelamento é abandono, não resolução."""
    return await _mudar_status(
        id_tarefa,
        config,
        tool_name="cancelar_tarefa",
        estados_validos=frozenset({"aberta"}),
        novo_status="cancelada",
        evento_jornada="journey_abandoned",
        evento_tarefa="task_cancelled",
        rotulo="cancelada",
        reason="user_cancelled",
    )


@tool
async def reabrir_tarefa(id_tarefa: int, config: RunnableConfig) -> str:
    """Reabre uma tarefa concluída ou cancelada."""
    return await _mudar_status(
        id_tarefa,
        config,
        tool_name="reabrir_tarefa",
        estados_validos=frozenset({"concluida", "cancelada"}),
        novo_status="aberta",
        evento_jornada="journey_reopened",
        evento_tarefa="task_reopened",
        rotulo="reaberta",
        reason="user_reopened",
    )


TOOLS_TAREFAS = [
    criar_tarefa,
    listar_tarefas,
    concluir_tarefa,
    cancelar_tarefa,
    reabrir_tarefa,
]
