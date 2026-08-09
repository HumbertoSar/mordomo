"""Tools do subagente Lembretes.

Padrões importantes:
  - `config: RunnableConfig` é injetado pelo LangChain → member_id vem do
    configurable (propagado desde o pipeline), nunca do LLM.
  - O LLM passa a EXPRESSÃO de tempo original ("amanhã às 8h"); quem resolve
    é `resolver_data` (determinístico). Se não entender → a tool devolve
    instrução para PEDIR ESCLARECIMENTO, não chuta.
  - Toda tool emite eventos de analytics (tool_called/tool_result)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from .. import scheduler
from ..analytics import emitir
from ..config import settings
from ..db.models import Reminder
from ..db.session import Sessao
from .datas import resolver_data


def _fmt(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(settings.tz_familia)).strftime("%d/%m às %H:%M")


@tool
async def criar_lembrete(texto: str, quando: str, config: RunnableConfig) -> str:
    """Cria um lembrete para o membro atual.

    Args:
        texto: o que lembrar (curto, ex.: "pagar o boleto da escola").
        quando: a expressão de tempo EXATAMENTE como o usuário disse, em
            português (ex.: "amanhã às 8h", "sexta que vem 19:30", "todo dia 5"
            ainda não é suportado — recorrência chega na fase 2).
    """
    member_id = config["configurable"]["member_id"]
    await emitir("tool_called", member_id, tool="criar_lembrete", quando=quando)
    dt = resolver_data(quando)
    if dt is None:
        await emitir("tool_result", member_id, tool="criar_lembrete", ok=False, motivo="data_nao_entendida")
        return (
            f"NÃO ENTENDI a expressão de tempo '{quando}'. "
            "Pergunte ao usuário a data e a hora exatas (não invente!)."
        )
    if dt <= datetime.now(UTC).astimezone(dt.tzinfo):
        await emitir("tool_result", member_id, tool="criar_lembrete", ok=False, motivo="data_no_passado")
        return f"'{quando}' resolveu para {_fmt(dt)}, que já passou. Confirme a data com o usuário."
    async with Sessao() as s:
        lembrete = Reminder(member_id=member_id, texto=texto, quando_utc=dt.astimezone(UTC))
        s.add(lembrete)
        await s.commit()
        await s.refresh(lembrete)
    scheduler.agendar(lembrete.id, lembrete.quando_utc)
    await emitir("reminder_created", member_id, reminder_id=lembrete.id, quando=_fmt(dt))
    return f"Lembrete #{lembrete.id} criado para {_fmt(dt)}: {texto}"


@tool
async def listar_lembretes(config: RunnableConfig) -> str:
    """Lista os lembretes pendentes do membro atual."""
    member_id = config["configurable"]["member_id"]
    await emitir("tool_called", member_id, tool="listar_lembretes")
    async with Sessao() as s:
        res = await s.execute(
            select(Reminder)
            .where(Reminder.member_id == member_id, Reminder.status == "pendente")
            .order_by(Reminder.quando_utc)
        )
        pendentes = list(res.scalars())
    if not pendentes:
        return "Nenhum lembrete pendente."
    return "\n".join(f"#{r.id} — {_fmt(r.quando_utc)}: {r.texto}" for r in pendentes)


@tool
async def cancelar_lembrete(id_lembrete: int, config: RunnableConfig) -> str:
    """Cancela um lembrete pelo número (id). Use listar_lembretes antes se precisar do id."""
    member_id = config["configurable"]["member_id"]
    await emitir("tool_called", member_id, tool="cancelar_lembrete", id=id_lembrete)
    async with Sessao() as s:
        lembrete = await s.get(Reminder, id_lembrete)
        if lembrete is None or lembrete.member_id != member_id:
            await emitir(
                "tool_result", member_id, tool="cancelar_lembrete", ok=False, motivo="nao_encontrado"
            )
            return f"Lembrete #{id_lembrete} não encontrado entre os seus lembretes."
        if lembrete.status != "pendente":
            await emitir(
                "tool_result", member_id, tool="cancelar_lembrete", ok=False, motivo="ja_resolvido"
            )
            return f"Lembrete #{id_lembrete} já está '{lembrete.status}'."
        lembrete.status = "cancelado"
        await s.commit()
    scheduler.cancelar_job(id_lembrete)
    await emitir("tool_result", member_id, tool="cancelar_lembrete", ok=True)
    return f"Lembrete #{id_lembrete} cancelado."


TOOLS_LEMBRETES = [criar_lembrete, listar_lembretes, cancelar_lembrete]
