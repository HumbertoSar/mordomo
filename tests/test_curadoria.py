"""Curadoria: colheita determinística de falhas reais → relatório + casos."""

import json

from mordomo.analytics import emitir
from mordomo.curadoria import coletar_achados, montar_casos_propostos, montar_relatorio


async def _semear_turno_com_falha_de_data(turn: str, expressao: str) -> None:
    await emitir("tool_called", 1, "1:t", turn, tool="criar_lembrete", quando=expressao)
    await emitir("tool_result", 1, "1:t", turn, tool="criar_lembrete", ok=False,
                 motivo="data_nao_entendida")


async def test_colhe_expressao_que_falhou_pareada_por_turno():
    await _semear_turno_com_falha_de_data("cur-t1", "no dia do jogo do flamengo")
    # ruído: turno OK não pode ser colhido
    await emitir("tool_called", 1, "1:t", "cur-t2", tool="criar_lembrete", quando="amanhã às 9h")
    await emitir("tool_result", 1, "1:t", "cur-t2", tool="criar_lembrete", ok=True)

    achados = await coletar_achados(dias=1)
    assert "no dia do jogo do flamengo" in achados["expressoes_de_data_falhas"]
    assert "amanhã às 9h" not in achados["expressoes_de_data_falhas"]


async def test_casos_propostos_sao_json_valido_com_esperado_para_humano():
    await _semear_turno_com_falha_de_data("cur-t3", "véspera de natal de manhãzinha")
    achados = await coletar_achados(dias=1)
    casos = montar_casos_propostos(achados)
    for linha in casos.split(",\n"):
        caso = json.loads(linha)
        assert caso["esperado"] == "PREENCHER"  # verdade humana, não palpite
        assert "caso REAL" in caso["nota"]


async def test_relatorio_semana_limpa_e_curto():
    achados = {
        "dias": 7, "turnos": 0, "expressoes_de_data_falhas": [],
        "motivos_de_falha": {}, "problemas_reportados": [],
        "erros_grafo": 0, "falhas_parse": 0, "p95_ms": None,
    }
    r = montar_relatorio(achados)
    assert "Semana limpa" in r and len(r) < 200


async def test_relatorio_com_achados_lista_expressoes():
    achados = {
        "dias": 7, "turnos": 30,
        "expressoes_de_data_falhas": ["no dia do jogo"],
        "motivos_de_falha": {"data_nao_entendida": 1, "nao_encontrado": 2},
        "problemas_reportados": ["erro no áudio"],
        "erros_grafo": 0, "falhas_parse": 0, "p95_ms": 8300.0,
    }
    r = montar_relatorio(achados)
    assert "no dia do jogo" in r
    assert "nao_encontrado (2)" in r
    assert "1 problema" in r
    assert "8.3s" in r
