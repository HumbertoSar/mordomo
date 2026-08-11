"""Helpers compartilhados pelas tools que resolvem datas (lembretes, agenda).

O texto de falha devolvido ao LLM é PROMPT: os dois agentes reagem a ele
("pergunte ao usuário…"). Quando era copiado nos dois arquivos, ajustar a
redação num lado mudava o comportamento dos agentes de formas diferentes —
agora há uma fonte só, e o eval mede uma frase só."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..analytics import emitir_de
from ..config import settings
from .datas import resolver_data


def fmt_data(dt: datetime, dia_semana: bool = False) -> str:
    formato = "%a %d/%m às %H:%M" if dia_semana else "%d/%m às %H:%M"
    return dt.astimezone(ZoneInfo(settings.tz_familia)).strftime(formato)


async def resolver_ou_instruir(
    quando: str, config, tool: str
) -> tuple[datetime | None, str | None]:
    """(dt, None) em sucesso; (None, instrução-para-o-LLM) em falha — já com o
    `tool_result` de falha emitido (motivo alimenta o eval de datas)."""
    dt = resolver_data(quando)
    if dt is None:
        await emitir_de(config, "tool_result", tool=tool, ok=False, motivo="data_nao_entendida")
        return None, (
            f"NÃO ENTENDI a expressão de tempo '{quando}'. "
            "Pergunte ao usuário a data e a hora exatas (não invente!)."
        )
    if dt <= datetime.now(UTC).astimezone(dt.tzinfo):
        await emitir_de(config, "tool_result", tool=tool, ok=False, motivo="data_no_passado")
        return None, (
            f"'{quando}' resolveu para {fmt_data(dt)}, que já passou. "
            "Confirme a data com o usuário."
        )
    return dt, None
