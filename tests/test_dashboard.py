"""Dashboard: as quatro seções montam contra o banco de teste, sem rede."""

from datetime import UTC, datetime, timedelta

from mordomo.analytics import emitir
from mordomo.reporting import queries
from mordomo.reporting.dashboard import _delta, _mini_trajetoria, _serie_evals, gerar_html


async def test_gerar_html_monta_as_quatro_secoes():
    html = await gerar_html(dias=7)
    assert "Analytics" in html
    assert "Produto" in html
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
    # tooltip nativo, sem JS: cada barra carrega um <title>
    assert svg.count("<title>") == 3


def test_dias_do_dashboard_valida_o_argumento():
    from mordomo.channels.telegram import _dias_do_dashboard

    assert _dias_do_dashboard(None) == 30          # sem argumento: padrão
    assert _dias_do_dashboard("") == 30
    assert _dias_do_dashboard("7") == 7
    assert _dias_do_dashboard("7 lixo depois") == 7
    assert _dias_do_dashboard("abc") is None       # inválido: handler dá a dica de uso
    assert _dias_do_dashboard("0") == 1            # fora da faixa: limite mais próximo
    assert _dias_do_dashboard("9999") == 365


def test_delta_compara_com_o_periodo_anterior():
    assert "▲" in _delta(12, 10) and "+20%" in _delta(12, 10)
    assert "▼" in _delta(8, 10)
    assert _delta(5, 0) == ""          # sem base de comparação, sem percentual
    assert "=" in _delta(10, 10)


async def test_produto_agrega_eventos_de_comando_sem_virar_orfao():
    desde = datetime.now(UTC) - timedelta(hours=1)
    orfaos_antes = await queries.orfaos(desde)

    # eventos que nascem FORA de um turno (comandos, jobs) — por desenho
    await emitir("document_stored", 1, "1:t", nome="boleto.pdf", mime="application/pdf")
    await emitir("dashboard_sent", 1, "1:t", dias=30)
    await emitir("curation_run", casos_propostos=2, problemas=1, issue_criada=True)
    await emitir("invite_created", 1, papel="adulto")
    # e o par pedido→issue, que nasce dentro de um turno
    await emitir("feature_requested", 1, "1:t", "t-prod-1",
                 titulo="tema escuro", categoria="funcionalidade")
    await emitir("feature_issue_created", 1, "1:t", "t-prod-1",
                 ok=True, categoria="funcionalidade")

    prod = await queries.produto(desde)
    assert prod["documentos"] >= 1
    assert prod["pedidos"] >= 1
    assert prod["issues_criadas"] >= 1
    assert prod["casos_propostos"] >= 2
    assert prod["convites"]["criados"] >= 1
    assert prod["dashboards_enviados"] >= 1
    assert any(cat == "funcionalidade" for cat, _ in prod["pedidos_por_categoria"])

    # nada disso pode inflar o KPI que vigia a instrumentação
    assert await queries.orfaos(desde) == orfaos_antes


async def test_resumo_respeita_a_janela_superior():
    # `ate` no passado: os eventos recém-emitidos ficam FORA do resumo anterior
    agora = datetime.now(UTC)
    anterior = await queries.resumo(agora - timedelta(days=2), agora - timedelta(days=1))
    assert anterior["turnos"] == 0 and anterior["usd"] == 0.0
