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

# Dia da semana em pt-BR, por índice de `weekday()` (0 = segunda). NÃO usamos
# `%a`: o strftime segue o locale do PROCESSO — na VPS (LC_ALL=C) sai "Mon", e
# instalar locale só para isto seria depender do sistema para formatar texto
# nosso. Determinismo aqui também tira do LLM a conta do dia da semana, que ele
# errou numa conversa real de 17/08/2026.
DIAS_DA_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def fmt_data(dt: datetime, dia_semana: bool = False) -> str:
    local = dt.astimezone(ZoneInfo(settings.tz_familia))
    corpo = local.strftime("%d/%m às %H:%M")
    # O dia da semana sai do instante JÁ convertido: 23h30 de domingo em São
    # Paulo é segunda em UTC, e converter tarde demais mudaria o dia.
    return f"{DIAS_DA_SEMANA[local.weekday()]} {corpo}" if dia_semana else corpo


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
