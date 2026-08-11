"""Recorrência de lembretes — parser determinístico pt-BR (mesma filosofia do
datas.py: o LLM passa a expressão como o usuário disse; quem decide é regex).

Três formas cobrem o uso de família:
    "todo dia às 8h"            → diária
    "toda segunda às 7h30"      → semanal
    "todo dia 5 às 9h"          → mensal (dia 31 vira o último dia do mês curto)

Sem hora → devolve Recorrencia com hora=None e a tool PERGUNTA (regra nº 3:
nunca inventar horário). Expressão que não é recorrência → None, e o fluxo cai
no resolver_data normal.

A regra é serializada na coluna reminders.recorrencia ("mensal:5@09:00") e o
scheduler REAGENDA a cada disparo em vez de marcar enviado."""

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import settings
from .datas import _normalizar

DIAS_SEMANA = {
    "segunda": 0, "terca": 1, "terça": 1, "quarta": 2, "quinta": 3,
    "sexta": 4, "sabado": 5, "sábado": 5, "domingo": 6,
}
_NOMES_DIA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

_HORA = r"(?:\s+às\s+(\d{1,2}:\d{2}))?"
# Ordem importa: "todo dia 5" tem que casar como MENSAL antes de "todo dia" (diária).
# O "dia" é OPCIONAL aqui porque o _normalizar do datas.py remove "dia " antes de
# número (conserto do "dia 5 de outubro") — quando a expressão chega, virou "todo 5".
_RGX_MENSAL = re.compile(r"^todo\s+(?:dia\s+)?(\d{1,2})(?:\s+de\s+cada\s+m[eê]s)?" + _HORA + r"$")
_RGX_DIARIA = re.compile(r"^tod[oa]s?\s+(?:os\s+)?dias?" + _HORA + r"$")
_RGX_SEMANAL = re.compile(
    r"^tod[oa]s?\s+(?:as\s+|os\s+)?(segunda|ter[cç]a|quarta|quinta|sexta|s[aá]bado|domingo)s?"
    r"(?:-feiras?)?" + _HORA + r"$"
)


@dataclass(frozen=True)
class Recorrencia:
    tipo: str          # "diaria" | "semanal" | "mensal"
    dia: int | None    # semanal: 0=segunda..6=domingo · mensal: dia do mês
    hora: str | None   # "HH:MM"; None = pedir esclarecimento

    def humana(self) -> str:
        h = f" às {self.hora}" if self.hora else ""
        if self.tipo == "diaria":
            return f"todo dia{h}"
        if self.tipo == "semanal":
            return f"toda {_NOMES_DIA[self.dia]}{h}"
        return f"todo dia {self.dia}{h}"


def _hora_padrao(h: str | None) -> str | None:
    """'7:30' → '07:30' (o _normalizar não põe zero à esquerda)."""
    if h is None:
        return None
    hh, _, mm = h.partition(":")
    return f"{int(hh):02d}:{mm}"


def interpretar(expressao: str) -> Recorrencia | None:
    e = _normalizar(expressao)
    if m := _RGX_MENSAL.match(e):
        dia = int(m.group(1))
        if not 1 <= dia <= 31:
            return None
        return Recorrencia("mensal", dia, _hora_padrao(m.group(2)))
    if m := _RGX_DIARIA.match(e):
        return Recorrencia("diaria", None, _hora_padrao(m.group(1)))
    if m := _RGX_SEMANAL.match(e):
        return Recorrencia("semanal", DIAS_SEMANA[m.group(1)], _hora_padrao(m.group(2)))
    return None


def serializar(r: Recorrencia) -> str:
    return f"{r.tipo}:{'' if r.dia is None else r.dia}@{r.hora}"


def desserializar(s: str) -> Recorrencia:
    cabeca, hora = s.split("@")
    tipo, _, dia = cabeca.partition(":")
    return Recorrencia(tipo, int(dia) if dia else None, hora)


def proxima_ocorrencia(r: Recorrencia, apos: datetime | None = None) -> datetime:
    """Próximo disparo DEPOIS de `apos` (default: agora), no fuso da família."""
    tz = ZoneInfo(settings.tz_familia)
    apos = datetime.now(tz) if apos is None else apos.astimezone(tz)
    hh, mm = (int(x) for x in (r.hora or "08:00").split(":"))

    if r.tipo == "diaria":
        alvo = apos.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return alvo if alvo > apos else alvo + timedelta(days=1)

    if r.tipo == "semanal":
        alvo = apos.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (r.dia - apos.weekday()) % 7
        alvo += timedelta(days=delta)
        return alvo if alvo > apos else alvo + timedelta(days=7)

    # mensal — "todo dia 31" num mês curto vira o último dia do mês
    ano, mes = apos.year, apos.month
    for _ in range(3):  # no máximo 2 avanços de mês resolvem qualquer caso
        dia = min(r.dia, calendar.monthrange(ano, mes)[1])
        alvo = datetime(ano, mes, dia, hh, mm, tzinfo=tz)
        if alvo > apos:
            return alvo
        mes = mes + 1 if mes < 12 else 1
        ano = ano if mes > 1 else ano + 1
    raise RuntimeError("proxima_ocorrencia não convergiu")  # pragma: no cover
