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
    # O dateparser não conhece "depois de amanhã"; conhece "em 2 dias".
    (r"\bdepois de amanhã\b", "em 2 dias"),
    # "que vem": as formas com unidade viram intervalo explícito…
    (r"\bsemana que vem\b", "em 1 semana"),
    (r"\bmês que vem\b", "em 1 mês"),
    (r"\bano que vem\b", "em 1 ano"),
    # …e "sexta que vem" vira só "sexta". DECISÃO DE PRODUTO: "que vem" = a
    # PRÓXIMA ocorrência (sexta que vem, numa segunda, é a sexta desta semana).
    # Humanos divergem nisso; escolhemos a leitura mais literal e a documentamos
    # aqui em vez de deixar o dateparser decidir sozinho.
    (r"\bque vem\b", ""),
    # "dia 5 de outubro" → "5 de outubro" (o "dia" solto atrapalha o parser).
    (r"\bdia\s+(?=\d)", ""),
    # "dia 21 desse mês" (caso real do Davi, 11/08): o dateparser não conhece
    # "desse/deste mês" — mas "21" sozinho ele resolve no mês corrente.
    (r"\b(?:desse|deste|do)\s+m[eê]s\b", ""),
]


# "8h da manhã", "7 da noite", "3 da tarde": o jeito mais comum de falar hora no
# Brasil — e o dateparser devolve None em todos. Descoberto na PRIMEIRA conversa
# real da família: `criar_lembrete` recebeu "amanhã às 8h da manhã" e registrou
# tool_result motivo=data_nao_entendida em product_events. O agente perguntou a
# hora em vez de inventar (regra nº 3 funcionando), mas a pergunta era desnecessária.
_PERIODOS = {"manhã": "am", "madrugada": "am", "tarde": "pm", "noite": "pm"}
_RGX_PERIODO = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(?:h|hs)?\s+d[ae]\s+(manhã|tarde|noite|madrugada)\b"
)


def _converter_periodo(e: str) -> str:
    """'8h da manhã' → '08:00' · '7 da noite' → '19:00' (24h explícito)."""

    def sub(m: re.Match) -> str:
        hora, minuto, periodo = int(m.group(1)), m.group(2) or "00", m.group(3)
        if _PERIODOS[periodo] == "pm" and hora < 12:
            hora += 12
        elif _PERIODOS[periodo] == "am" and hora == 12:
            hora = 0
        return f"{hora:02d}:{minuto}"

    return _RGX_PERIODO.sub(sub, e)


def _normalizar(expressao: str) -> str:
    e = expressao.strip().lower()
    for rgx, sub in _SUBSTITUICOES:
        e = re.sub(rgx, sub, e)
    e = re.sub(r"\b(\d{1,2})h(\d{2})\b", r"\1:\2", e)  # 19h30 → 19:30
    e = re.sub(r"\b(\d{1,2})hs?\b", r"\1:00", e)       # 8h / 8hs → 8:00
    e = re.sub(r"\b([àa]s)\s+(\d{1,2})\b(?!\s*:)", r"\1 \2:00", e)  # às 18 → às 18:00
    e = re.sub(r"\bao\s+(\d{1,2}:\d{2})\b", r"às \1", e)            # ao 12:00 → às 12:00
    e = re.sub(r"\bà\s+(?=\d{1,2}:\d{2})", "às ", e)                # à 00:00 → às 00:00
    e = _converter_periodo(e)                          # 8:00 da manhã → 08:00
    return re.sub(r"\s+", " ", e).strip()              # remover "que vem" deixa espaço duplo


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
    # Meia-noite SILENCIOSA = o usuário não disse a hora ("dia 21" → 00:00 por
    # default do parser). Regra nº 3: sem hora, PERGUNTA — devolvemos None.
    # Meia-noite EXPLÍCITA sobrevive: "meia-noite" já virou "00:00" no texto.
    if dt.hour == 0 and dt.minute == 0 and "00:00" not in expressao:
        return None
    return dt.astimezone(tz)
