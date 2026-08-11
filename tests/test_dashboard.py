"""Dashboard: as três seções montam contra o banco de teste, sem rede."""

from mordomo.reporting.dashboard import _mini_trajetoria, _serie_evals, gerar_html


async def test_gerar_html_monta_as_tres_secoes():
    html = await gerar_html(dias=7)
    assert "Analytics" in html
    assert "Observabilidade" in html
    assert "Evaluation" in html
    # autocontido: nada de CDN/script externo
    assert "<script" not in html and "cdn" not in html.lower()
    # o arquivo abre DIRETO do Telegram, sem <head> de ninguém: charset e
    # viewport são obrigatórios (sem eles: mojibake no Safari + zoom-out no celular)
    assert html.startswith('<meta charset="utf-8">')
    assert 'name="viewport"' in html


def test_serie_evals_le_o_historico_versionado():
    serie = _serie_evals()
    nomes = {e["nome"] for e in serie}
    # o history.csv é versionado — os três evals do projeto têm runs salvos
    assert {"datas_ptbr", "roteamento", "qualidade_resposta"} <= nomes
    for e in serie:
        assert 0.0 <= e["acuracia"] <= 1.0 and e["trajetoria"]


def test_mini_trajetoria_e_svg_valido():
    svg = _mini_trajetoria([0.5, 0.8, 1.0])
    assert svg.startswith("<svg") and svg.count("<rect") == 3
