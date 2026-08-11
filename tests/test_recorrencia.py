"""Recorrência: parser determinístico + cálculo da próxima ocorrência."""

from datetime import datetime
from zoneinfo import ZoneInfo

from mordomo.tools.recorrencia import (
    Recorrencia,
    desserializar,
    interpretar,
    proxima_ocorrencia,
    serializar,
)

TZ = ZoneInfo("America/Sao_Paulo")
BASE = datetime(2026, 8, 10, 9, 0, tzinfo=TZ)  # segunda-feira, 09:00


# ── Parser ───────────────────────────────────────────────────────────────


def test_diaria_com_hora():
    r = interpretar("todo dia às 8h")
    assert r == Recorrencia("diaria", None, "08:00")


def test_semanal_variacoes():
    assert interpretar("toda segunda às 7h30") == Recorrencia("semanal", 0, "07:30")
    assert interpretar("toda segunda-feira às 7h30") == Recorrencia("semanal", 0, "07:30")
    assert interpretar("todo sábado às 10h") == Recorrencia("semanal", 5, "10:00")


def test_mensal_e_precedencia_sobre_diaria():
    """'todo dia 5' tem que ser MENSAL, não diária com lixo."""
    assert interpretar("todo dia 5 às 9h") == Recorrencia("mensal", 5, "09:00")
    assert interpretar("todo dia 5 de cada mês às 9h") == Recorrencia("mensal", 5, "09:00")


def test_sem_hora_devolve_hora_none():
    """Regra reconhecida sem hora → a tool pergunta, não inventa."""
    r = interpretar("toda sexta")
    assert r is not None and r.tipo == "semanal" and r.hora is None


def test_nao_recorrencia_devolve_none():
    assert interpretar("amanhã às 8h") is None
    assert interpretar("sexta às 19h") is None


def test_serializacao_roundtrip():
    for r in [Recorrencia("diaria", None, "08:00"), Recorrencia("semanal", 4, "19:00"),
              Recorrencia("mensal", 31, "09:00")]:
        assert desserializar(serializar(r)) == r


# ── Próxima ocorrência ───────────────────────────────────────────────────


def test_diaria_hoje_se_ainda_nao_passou():
    dt = proxima_ocorrencia(Recorrencia("diaria", None, "20:00"), BASE)
    assert (dt.day, dt.hour) == (10, 20)


def test_diaria_amanha_se_ja_passou():
    dt = proxima_ocorrencia(Recorrencia("diaria", None, "08:00"), BASE)
    assert (dt.day, dt.hour) == (11, 8)


def test_semanal_mesmo_dia_ja_passou_vai_para_proxima_semana():
    # BASE é segunda 09:00; "toda segunda às 07:30" → próxima segunda (17/08)
    dt = proxima_ocorrencia(Recorrencia("semanal", 0, "07:30"), BASE)
    assert (dt.day, dt.hour, dt.minute) == (17, 7, 30)


def test_mensal_dia_31_vira_ultimo_dia_do_mes_curto():
    # base em 31/08 09:00 → setembro não tem 31 → 30/09
    base = datetime(2026, 8, 31, 10, 0, tzinfo=TZ)
    dt = proxima_ocorrencia(Recorrencia("mensal", 31, "09:00"), base)
    assert (dt.month, dt.day) == (9, 30)