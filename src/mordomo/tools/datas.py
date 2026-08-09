"""Datas em pt-BR — abordagem híbrida (ADR: LLM extrai a EXPRESSÃO, parser
determinístico resolve). "sexta que vem", "amanhã às 8h" etc. são a fonte
clássica de erro de assistentes em português — por isso este módulo tem
dataset de eval dedicado (evals/datasets/datas_ptbr.json).

Devolve None quando não entende — o subagente então PEDE ESCLARECIMENTO em
vez de chutar (chutar data errada é o pior erro possível num mordomo)."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import dateparser

from ..config import settings

# O dateparser tropeça em formas MUITO brasileiras ("daqui a 2 horas" → None;
# "às 8h" → interpreta 8h como duração/dia do mês!). Normalizamos antes.
# Descoberto rodando `make evals` — exatamente o loop que este projeto ensina.
_SUBSTITUICOES: list[tuple[str, str]] = [
    (r"\bdaqui a\b", "em"),
    (r"\bdaqui\b", "em"),
    (r"\bdentro de\b", "em"),
    (r"\bmeio[- ]dia\b", "12:00"),
    (r"\bmeia[- ]noite\b", "00:00"),
]


def _normalizar(expressao: str) -> str:
    e = expressao.strip().lower()
    for rgx, sub in _SUBSTITUICOES:
        e = re.sub(rgx, sub, e)
    e = re.sub(r"\b(\d{1,2})h(\d{2})\b", r"\1:\2", e)  # 19h30 → 19:30
    e = re.sub(r"\b(\d{1,2})hs?\b", r"\1:00", e)       # 8h / 8hs → 8:00
    e = re.sub(r"\b([àa]s)\s+(\d{1,2})\b(?!\s*:)", r"\1 \2:00", e)  # às 18 → às 18:00
    e = re.sub(r"\bao\s+(\d{1,2}:\d{2})\b", r"às \1", e)            # ao 12:00 → às 12:00
    return e


def resolver_data(expressao: str, base: datetime | None = None) -> datetime | None:
    """Expressão pt-BR → datetime com timezone da família, ou None."""
    tz = ZoneInfo(settings.tz_familia)
    expressao = _normalizar(expressao)
    ajustes = {
        "TIMEZONE": settings.tz_familia,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",   # "sexta" = a próxima sexta
        "DATE_ORDER": "DMY",             # 05/09 = 5 de setembro
    }
    if base is not None:
        # dateparser espera base ingênua no fuso local
        ajustes["RELATIVE_BASE"] = base.astimezone(tz).replace(tzinfo=None)
    dt = dateparser.parse(expressao, languages=["pt"], settings=ajustes)
    if dt is None:
        return None
    return dt.astimezone(tz)
