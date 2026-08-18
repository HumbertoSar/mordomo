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


def test_periodo_da_manha():
    """Regressão do primeiro bug encontrado em conversa real com a família."""
    dt = resolver_data("amanhã às 8h da manhã", base=BASE)
    assert dt is not None
    assert (dt.day, dt.hour, dt.minute) == (11, 8, 0)


def test_periodo_da_noite_vira_24h():
    dt = resolver_data("hoje às 7 da noite", base=BASE)
    assert dt is not None
    assert (dt.day, dt.hour) == (10, 19)


def test_depois_de_amanha():
    dt = resolver_data("depois de amanhã ao meio-dia", base=BASE)
    assert dt is not None
    assert (dt.day, dt.hour) == (12, 12)


def test_que_vem_e_a_proxima_ocorrencia():
    """Decisão de produto: 'sexta que vem', numa segunda, é a sexta desta semana."""
    dt = resolver_data("sexta que vem às 19h", base=BASE)
    assert dt is not None
    assert (dt.day, dt.hour) == (14, 19)


def test_dia_N_de_mes():
    dt = resolver_data("dia 5 de outubro às 15h", base=BASE)
    assert dt is not None
    assert (dt.month, dt.day, dt.hour) == (10, 5, 15)


def test_periodo_sem_hora_continua_ambiguo():
    """'de manhã' sozinho NÃO tem hora — pedir esclarecimento segue sendo o certo."""
    assert resolver_data("amanhã de manhã", base=BASE) is None


def test_expressao_sem_sentido_devolve_none():
    assert resolver_data("xyzzy plugh", base=BASE) is None


def test_resultado_tem_timezone_da_familia():
    dt = resolver_data("amanhã às 08:00", base=BASE)
    assert dt is not None and dt.tzinfo is not None
    assert dt.utcoffset() == BASE.utcoffset()


# ── intervalos naturais (canário real da Fatia A, 18/08/2026) ────────────
# O LLM fatiou "amanhã das 18h às 18h30" e mandou `quando="amanhã das 18h"`,
# `ate="18h30"`. O fragmento com preposição de intervalo ("das", "de",
# "começando", "entre") voltava None e o Mordomo pedia a hora que o usuário
# JÁ tinha dito.


def test_fragmento_com_das():
    dt = resolver_data("amanhã das 18h", base=BASE)
    assert dt is not None
    assert (dt.day, dt.hour, dt.minute) == (11, 18, 0)


def test_fragmento_com_comecando():
    dt = resolver_data("amanhã começando 20h30", base=BASE)
    assert dt is not None
    assert (dt.day, dt.hour, dt.minute) == (11, 20, 30)


def test_fragmento_com_a_partir_das():
    dt = resolver_data("amanhã a partir das 10h", base=BASE)
    assert dt is not None
    assert (dt.day, dt.hour) == (11, 10)


def test_frase_de_intervalo_inteira_resolve_o_comeco():
    """A frase natural inteira também chega ao parser (o LLM nem sempre fatia)."""
    for frase in (
        "amanhã das 18h às 18h30",
        "amanhã de 18h a 18h30",
        "amanhã entre 18h e 18h30",
        "amanhã começando 18h e terminando 18h30",
    ):
        dt = resolver_data(frase, base=BASE)
        assert dt is not None, frase
        assert (dt.day, dt.hour, dt.minute) == (11, 18, 0), frase


def test_de_outubro_nao_vira_hora():
    """A normalização de 'de/das' só age diante de HORA — não de mês."""
    dt = resolver_data("dia 5 de outubro às 15h", base=BASE)
    assert dt is not None
    assert (dt.month, dt.day, dt.hour) == (10, 5, 15)


def test_intervalo_invertido_nao_vira_instante_valido():
    """Quem fornece começo e fim inválidos não pode ter o fim ignorado por
    consumidores de ``resolver_data`` (por exemplo, lembretes)."""
    assert resolver_data("amanhã das 20h às 19h", base=BASE) is None


def test_tres_horarios_nao_sao_truncados_silenciosamente():
    """Uma expressão ambígua não pode descartar o terceiro horário e aceitar os
    dois primeiros como se fossem o pedido inteiro."""
    from mordomo.tools.datas import resolver_intervalo

    intervalo = resolver_intervalo("amanhã das 9h às 10h e 11h", base=BASE)
    assert intervalo.motivo == "sem_intervalo"
