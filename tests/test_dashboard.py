"""Dashboard: as quatro seções montam contra o banco de teste, sem rede."""

from datetime import UTC, datetime, timedelta

import pytest

from mordomo.analytics import emitir
from mordomo.reporting import queries
from mordomo.reporting.dashboard import _delta, _mini_trajetoria, gerar_html


async def test_gerar_html_monta_as_cinco_secoes():
    html = await gerar_html(dias=7)
    assert "Analytics" in html
    assert "Canais" in html
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
    serie = queries.serie_evals()
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


def test_injetar_dashboard_troca_so_o_bloco_embutido():
    from mordomo.reporting.publicar import injetar_dashboard

    pagina = '<p>antes</p><script type="text/html" id="dash-src">VELHO</script><p>depois</p>'
    saida = injetar_dashboard(pagina, "<div>NOVO</div>")
    assert "VELHO" not in saida
    assert "<div>NOVO</div>" in saida
    assert saida.startswith("<p>antes</p>") and saida.endswith("<p>depois</p>")


def test_injetar_dashboard_recusa_painel_com_script():
    # se o dashboard ganhar JS um dia, embutir assim quebraria a página em
    # silêncio — o erro tem que ser alto
    from mordomo.reporting.publicar import injetar_dashboard

    pagina = '<script type="text/html" id="dash-src"></script>'
    with pytest.raises(ValueError, match="script"):
        injetar_dashboard(pagina, "<script>alert(1)</script>")


async def test_publicar_monta_a_pasta_sem_sujar_o_working_tree(tmp_path):
    # na VPS, escrever em docs/ faria o próximo `git pull` abortar — foi o que
    # aconteceu com o backup.sh. Por padrão, publicar() só toca em --saida.
    from mordomo.reporting import publicar

    antes = (publicar.PAGINA.read_bytes(), publicar.PAINEL_VERSIONADO.read_bytes())
    destino = await publicar.montar(7, tmp_path / "publico", atualizar_docs=False)

    assert (destino / "index.html").exists() and (destino / "dashboard.html").exists()
    painel = (destino / "dashboard.html").read_text(encoding="utf-8")
    pagina = (destino / "index.html").read_text(encoding="utf-8")
    assert "gestão à vista" in painel
    assert painel in pagina  # o painel do dia entrou no lugar da cópia velha
    assert (publicar.PAGINA.read_bytes(), publicar.PAINEL_VERSIONADO.read_bytes()) == antes


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


async def test_canais_medem_entrega_leitura_e_adocao():
    """O que só o WhatsApp informa: um wamid gera VÁRIOS statuses, e é a
    diferença entre os carimbos que vira "tempo até ser lida"."""
    desde = datetime.now(UTC) - timedelta(hours=1)
    base = int(datetime.now(UTC).timestamp())

    await emitir("message_received", 1, "1:t", "t-canal-1", canal="whatsapp", tamanho=20)
    await emitir("message_received", 2, "2:t", "t-canal-2", canal="telegram", tamanho=20)
    await emitir("message_sent", 1, "1:t", "t-canal-1", canal="whatsapp",
                 tamanho=80, wamid="wamid.lida")
    for status, quando in (("sent", base), ("delivered", base + 3), ("read", base + 63)):
        await emitir("message_status", 1, "1:t", canal="whatsapp", status=status,
                     wamid="wamid.lida", erro="", ts_canal=quando)
    # uma que a Meta recusou (fora da janela de 24h sem template: erro 131047)
    await emitir("message_status", 1, "1:t", canal="whatsapp", status="failed",
                 wamid="wamid.falha", erro="131047", ts_canal=base)
    await emitir("proactive_channel", 1, canal="whatsapp", modo="template",
                 template="lembrete_v1")

    c = await queries.canais(desde)
    assert c["por_canal"]["whatsapp"]["recebidas"] >= 1
    assert c["por_canal"]["telegram"]["recebidas"] >= 1

    wa = c["whatsapp"]
    assert wa["lidas"] >= 1 and wa["falhas"] >= 1
    # 3 statuses da MESMA mensagem contam como UMA mensagem
    assert wa["com_status"] >= 2
    assert wa["p50_ate_leitura_s"] is not None and wa["p50_ate_leitura_s"] >= 60
    assert ("131047", 1) in wa["erros"]
    assert ("template", 1) in wa["proativos_por_modo"]


async def test_pedido_v1_sem_categoria_conta_como_funcionalidade():
    # antes do "Pedidos v2" o evento não tinha `categoria` — não pode virar "?"
    desde = datetime.now(UTC) - timedelta(hours=1)
    antes = dict(await queries.produto(desde))["pedidos_por_categoria"]
    n_antes = dict(antes).get("funcionalidade", 0)

    await emitir("feature_requested", 1, "1:t", "t-prod-v1", titulo="pedido v1")

    depois = dict((await queries.produto(desde))["pedidos_por_categoria"])
    assert depois.get("funcionalidade", 0) == n_antes + 1
    assert "?" not in depois


async def test_orfaos_por_tipo_diagnostica_o_kpi():
    desde = datetime.now(UTC) - timedelta(hours=1)
    total_antes = await queries.orfaos(desde)

    # um message_sent sem turn_id — como os do 1º dia de uso da família
    await emitir("message_sent", 1, "1:t", canal="telegram", tamanho=10)

    assert await queries.orfaos(desde) == total_antes + 1
    detalhe = await queries.orfaos_por_tipo(desde)
    por_tipo = {tipo: (n, ultimo) for tipo, n, ultimo in detalhe}
    assert "message_sent" in por_tipo
    n, ultimo = por_tipo["message_sent"]
    assert n >= 1
    # a data vem no fuso da família, formato ISO — igual ao resto das queries
    assert ultimo == queries._dia(datetime.now(UTC))


async def test_migracao_e_sinais_de_canal_aparecem_no_placar():
    desde = datetime.now(UTC) - timedelta(hours=1)
    orfaos_antes = await queries.orfaos(desde)

    # todos nascem fora de turno, por desenho
    await emitir("connect_created", 1)
    await emitir("connect_used", 1, canal="whatsapp")
    await emitir("proactive_failed", 2, canais=["whatsapp"], tamanho=10)
    await emitir("message_duplicated", canal="whatsapp", wamid="wamid.teste")

    prod = await queries.produto(desde)
    assert prod["conexoes"]["criadas"] >= 1
    assert prod["conexoes"]["usadas"] >= 1

    s = await queries.saude(desde)
    assert s["proativos_falhos"] >= 1
    assert s["reentregas_meta"] >= 1

    # e nenhum deles infla o KPI que vigia a instrumentação
    assert await queries.orfaos(desde) == orfaos_antes


async def test_custo_de_template_cobra_entregue_e_teto_sem_rastreio():
    desde = datetime.now(UTC) - timedelta(hours=1)
    base = (await queries.canais(desde))["whatsapp"]["templates_cobrados"]

    # rastreado e entregue: cobra
    await emitir("proactive_channel", 1, canal="whatsapp", modo="template", wamid="wamid.c1")
    await emitir("message_status", 1, canal="whatsapp", status="delivered",
                 wamid="wamid.c1", ts_canal=100)
    # rastreado SEM confirmação de entrega: não cobra
    await emitir("proactive_channel", 1, canal="whatsapp", modo="template", wamid="wamid.c2")
    # antigo, sem rastreio: cobra como teto
    await emitir("proactive_channel", 1, canal="whatsapp", modo="template")

    wa = (await queries.canais(desde))["whatsapp"]
    assert wa["templates_cobrados"] == base + 2
    esperado = wa["templates_cobrados"] * queries.PRECO_TEMPLATE_WHATSAPP_USD
    assert abs(wa["custo_templates_usd"] - esperado) < 1e-9


async def test_latencia_por_canal_casa_pelo_turn_id():
    desde = datetime.now(UTC) - timedelta(hours=1)
    await emitir("message_received", 1, "1:t", "t-lat-wa", canal="whatsapp", tamanho=10)
    await emitir("turn_completed", 1, "1:t", "t-lat-wa", ok=True, latencia_ms=1234.0)

    lpc = await queries.latencia_por_canal(desde)
    assert lpc["whatsapp"]["turnos"] >= 1
    assert lpc["whatsapp"]["p50_ms"] is not None


async def test_resumo_respeita_a_janela_superior():
    # `ate` no passado: os eventos recém-emitidos ficam FORA do resumo anterior
    agora = datetime.now(UTC)
    anterior = await queries.resumo(agora - timedelta(days=2), agora - timedelta(days=1))
    assert anterior["turnos"] == 0 and anterior["usd"] == 0.0
