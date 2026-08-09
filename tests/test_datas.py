"""Casos SEGUROS de datas pt-BR (CI não pode ser flaky).

Os casos difíceis/ambíguos ("sexta que vem", "depois do almoço") ficam no
EVAL (evals/datasets/datas_ptbr.json), onde errar é achado, não build quebrado."""

from datetime import datetime
from zoneinfo import ZoneInfo

from mordomo.tools.datas import resolver_data

TZ = ZoneInfo("America/Sao_Paulo")
BASE = datetime(2026, 8, 10, 9, 0, tzinfo=TZ)  # segunda-feira, 09:00


def test_amanha_com_hora():
    dt = resolver_data("amanhã às 20:00", base=BASE)
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 8, 11, 20, 0)


def test_daqui_a_2_horas():
    dt = resolver_data("daqui a 2 horas", base=BASE)
    assert dt is not None
    assert (dt.day, dt.hour) == (10, 11)


def test_data_absoluta_dmy():
    dt = resolver_data("15/09/2026 10:00", base=BASE)
    assert dt is not None
    assert (dt.month, dt.day, dt.hour) == (9, 15, 10)


def test_expressao_sem_sentido_devolve_none():
    assert resolver_data("xyzzy plugh", base=BASE) is None


def test_resultado_tem_timezone_da_familia():
    dt = resolver_data("amanhã às 08:00", base=BASE)
    assert dt is not None and dt.tzinfo is not None
    assert dt.utcoffset() == BASE.utcoffset()
