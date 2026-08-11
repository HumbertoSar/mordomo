"""Tools do subagente Lembretes.

Padrões importantes:
  - `config: RunnableConfig` é injetado pelo LangChain → member_id vem do
    configurable (propagado desde o pipeline), nunca do LLM.
  - O LLM passa a EXPRESSÃO de tempo original ("amanhã às 8h"); quem resolve
    é `resolver_data` (determinístico). Se não entender → a tool devolve
    instrução para PEDIR ESCLARECIMENTO, não chuta.
  - Toda tool emite analytics por `emitir_de(config, ...)`, que já carrega
    member/session/turn — evento sem turn_id não entra em funil nenhum.
  - Todo caminho de saída emite `tool_result`, inclusive os de falha: o motivo
    ("data_nao_entendida") é o que vira backlog no eval de datas."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from .. import scheduler
from ..analytics import emitir_de
from ..config import settings
from ..db.models import Reminder
from ..db.session import Sessao
from . import recorrencia
from .datas import resolver_data


def _fmt(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(settings.tz_familia)).strftime("%d/%m às %H:%M")


@tool
async def criar_lembrete(texto: str, quando: str, config: RunnableConfig) -> str:
    """Cria um lembrete para o membro atual — único OU recorrente.

    Args:
        texto: o que lembrar (curto, ex.: "pagar o boleto da escola").
        quando: a expressão de tempo EXATAMENTE como o usuário disse, em
            português. Único: "amanhã às 8h", "sexta que vem 19:30".
            Recorrente (suportado!): "todo dia às 8h", "toda segunda às 7h30",
            "todo dia 5 às 9h" — a tool detecta e reagenda sozinha.
    """
    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="criar_lembrete", quando=quando)

    # Recorrência primeiro: "todo dia 5 às 9h" não é uma data, é uma regra.
    if (regra := recorrencia.interpretar(quando)) is not None:
        if regra.hora is None:
            await emitir_de(
                config, "tool_result", tool="criar_lembrete", ok=False, motivo="recorrencia_sem_hora"
            )
            return (
                f"Entendi a recorrência ({regra.humana()}), mas FALTA A HORA. "
                "Pergunte ao usuário que horas o lembrete deve tocar."
            )
        dt = recorrencia.proxima_ocorrencia(regra)
        async with Sessao() as s:
            lembrete = Reminder(
                member_id=member_id,
                texto=texto,
                quando_utc=dt.astimezone(UTC),
                recorrencia=recorrencia.serializar(regra),
            )
            s.add(lembrete)
            await s.commit()
            await s.refresh(lembrete)
        scheduler.agendar(lembrete.id, lembrete.quando_utc)
        await emitir_de(config, "tool_result", tool="criar_lembrete", ok=True, recorrente=True)
        await emitir_de(
            config, "reminder_created", reminder_id=lembrete.id, recorrencia=regra.humana()
        )
        return (
            f"Lembrete recorrente #{lembrete.id} criado ({regra.humana()}): {texto}. "
            f"Próximo toque: {_fmt(dt)}."
        )

    dt = resolver_data(quando)
    if dt is None:
        await emitir_de(
            config, "tool_result", tool="criar_lembrete", ok=False, motivo="data_nao_entendida"
        )
        return (
            f"NÃO ENTENDI a expressão de tempo '{quando}'. "
            "Pergunte ao usuário a data e a hora exatas (não invente!)."
        )
    if dt <= datetime.now(UTC).astimezone(dt.tzinfo):
        await emitir_de(
            config, "tool_result", tool="criar_lembrete", ok=False, motivo="data_no_passado"
        )
        return f"'{quando}' resolveu para {_fmt(dt)}, que já passou. Confirme a data com o usuário."
    async with Sessao() as s:
        lembrete = Reminder(member_id=member_id, texto=texto, quando_utc=dt.astimezone(UTC))
        s.add(lembrete)
        await s.commit()
        await s.refresh(lembrete)
    scheduler.agendar(lembrete.id, lembrete.quando_utc)
    await emitir_de(config, "tool_result", tool="criar_lembrete", ok=True)
    await emitir_de(config, "reminder_created", reminder_id=lembrete.id, quando=_fmt(dt))
    return f"Lembrete #{lembrete.id} criado para {_fmt(dt)}: {texto}"


@tool
async def listar_lembretes(config: RunnableConfig) -> str:
    """Lista os lembretes pendentes do membro atual."""
    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="listar_lembretes")
    async with Sessao() as s:
        res = await s.execute(
            select(Reminder)
            .where(Reminder.member_id == member_id, Reminder.status == "pendente")
            .order_by(Reminder.quando_utc)
        )
        pendentes = list(res.scalars())
    await emitir_de(config, "tool_result", tool="listar_lembretes", ok=True, n=len(pendentes))
    if not pendentes:
        return "Nenhum lembrete pendente."

    def _linha(r: Reminder) -> str:
        base = f"#{r.id} — {_fmt(r.quando_utc)}: {r.texto}"
        if r.recorrencia:
            base += f" (🔁 {recorrencia.desserializar(r.recorrencia).humana()})"
        return base

    return "\n".join(_linha(r) for r in pendentes)


@tool
async def cancelar_lembrete(id_lembrete: int, config: RunnableConfig) -> str:
    """Cancela um lembrete pelo número (id). Use listar_lembretes antes se precisar do id."""
    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="cancelar_lembrete", id=id_lembrete)
    async with Sessao() as s:
        lembrete = await s.get(Reminder, id_lembrete)
        if lembrete is None or lembrete.member_id != member_id:
            await emitir_de(
                config, "tool_result", tool="cancelar_lembrete", ok=False, motivo="nao_encontrado"
            )
            return f"Lembrete #{id_lembrete} não encontrado entre os seus lembretes."
        if lembrete.status != "pendente":
            await emitir_de(
                config, "tool_result", tool="cancelar_lembrete", ok=False, motivo="ja_resolvido"
            )
            return f"Lembrete #{id_lembrete} já está '{lembrete.status}'."
        lembrete.status = "cancelado"
        await s.commit()
    scheduler.cancelar_job(id_lembrete)
    await emitir_de(config, "tool_result", tool="cancelar_lembrete", ok=True)
    return f"Lembrete #{id_lembrete} cancelado."


TOOLS_LEMBRETES = [criar_lembrete, listar_lembretes, cancelar_lembrete]
