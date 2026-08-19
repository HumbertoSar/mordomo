"""Dashboard: as camadas montam contra o banco de teste, sem rede."""

from datetime import UTC, datetime, timedelta

import pytest

from mordomo.analytics import emitir
from mordomo.db.models import ProductEvent
from mordomo.db.session import Sessao
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


def test_estatisticas_do_codigo_conta_a_arvore_real():
    # limites frouxos de propósito: o projeto CRESCE, o teste não pode quebrar
    # a cada tool ou ADR novo — só provar que a contagem é de verdade, não zero
    from mordomo.reporting.publicar import estatisticas_do_codigo

    s = estatisticas_do_codigo()
    assert s["linhas_src"] > 3000
    assert s["testes"] >= 170
    assert s["nos_grafo"] == 6  # supervisor + 5 especialistas — muda só com novo agente
    assert s["ferramentas"] >= 11
    assert s["canais"] == 2  # TelegramAdapter + WhatsAppAdapter
    assert s["adrs"] >= 9
    assert s["tipos_evento"] >= 20


def test_fmt_milhar_separa_a_brasileira():
    from mordomo.reporting.publicar import _fmt_milhar

    assert _fmt_milhar(999) == "999"
    assert _fmt_milhar(1234) == "1.234"
    assert _fmt_milhar(12345) == "12.345"


def _pagina_ledger_falsa() -> str:
    ids = [
        "lg-linhas", "lg-testes", "lg-nos", "lg-tools", "lg-canais",
        "lg-adrs", "lg-custo", "lg-eventos",
    ]
    return "".join(f'<dd id="{i}">velho</dd>' for i in ids) + '<p id="ledger-data">dados de --</p>'


def test_injetar_estatisticas_preenche_o_ledger():
    from mordomo.reporting.publicar import injetar_estatisticas

    stats = {
        "linhas_src": 5646, "testes": 176, "nos_grafo": 5, "ferramentas": 11,
        "canais": 2, "adrs": 9, "tipos_evento": 27,
    }
    saida = injetar_estatisticas(
        _pagina_ledger_falsa(), stats, custo_por_turno=0.00712, data="15/08/2026"
    )
    assert '<dd id="lg-linhas">5.646</dd>' in saida
    assert '<dd id="lg-testes">176</dd>' in saida
    assert '<dd id="lg-custo">~US$ 0.0071</dd>' in saida
    assert '<p id="ledger-data">dados de 15/08/2026</p>' in saida


def test_injetar_estatisticas_sem_turno_no_periodo_nao_inventa_custo():
    from mordomo.reporting.publicar import injetar_estatisticas

    stats = dict.fromkeys(
        ["linhas_src", "testes", "nos_grafo", "ferramentas", "canais", "adrs", "tipos_evento"], 0
    )
    saida = injetar_estatisticas(
        _pagina_ledger_falsa(), stats, custo_por_turno=None, data="15/08/2026"
    )
    assert '<dd id="lg-custo">—</dd>' in saida


async def test_dashboard_mostra_resolucao_quando_ha_jornadas():
    await emitir(
        "journey_started",
        1,
        "1:hoje",
        "turn-dashboard-journey-1",
        journey_id="dashboard-journey-1",
        journey_type="task",
        loads=["mental"],
    )
    await emitir(
        "journey_resolved",
        1,
        "1:hoje",
        "turn-dashboard-journey-2",
        journey_id="dashboard-journey-1",
    )

    html = await gerar_html(dias=1)

    assert "Jornadas familiares" in html
    assert "taxa de resolução" in html
    assert "100%" in html


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

    # o ledger saiu do texto digitado à mão — o nº de testes bate com a árvore
    # real, e "20+" (o vago de antes) não sobrevive
    import re

    from mordomo.reporting.publicar import estatisticas_do_codigo

    m = re.search(r'id="lg-testes">(\d+)<', pagina)
    assert m and int(m.group(1)) == estatisticas_do_codigo()["testes"]
    assert 'id="lg-eventos">20+<' not in pagina
    assert re.search(r'id="ledger-data">dados de \d\d/\d\d/\d\d\d\d<', pagina)


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


async def test_saude_mostra_timeouts_llm_como_subconjunto_dos_erros():
    desde = datetime.now(UTC) - timedelta(hours=1)
    antes = await queries.saude(desde)

    await emitir("error", 1, "1:t", "t-timeout", onde="grafo",
                 tentativa=1, efeito=False, motivo="timeout_llm")

    depois = await queries.saude(desde)
    assert depois["timeouts_llm"] == antes.get("timeouts_llm", 0) + 1
    assert depois["erros_grafo"] == antes["erros_grafo"] + 1
    assert "timeouts de LLM (incluídos nos erros acima)" in (await gerar_html(dias=1))


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


# ── resultados comprovados (turno ≠ resultado ≠ jornada) ────────────────


def _linha(resultados: dict, capability: str, operation: str) -> dict:
    achadas = [
        r for r in resultados["por_operacao"]
        if r["capability"] == capability and r["operation"] == operation
    ]
    assert len(achadas) == 1, f"{capability}/{operation} não apareceu uma única vez"
    return achadas[0]


async def test_leitura_so_conta_como_resultado_com_resposta_no_mesmo_turno():
    """Ler a agenda e não conseguir responder não ajudou ninguém — o valor de
    uma leitura é a resposta chegando, não a query voltando."""
    desde = datetime.now(UTC)

    await emitir("tool_called", 1, "1:t", "t-res-le1", tool="listar_agenda", dias=7)
    await emitir("tool_result", 1, "1:t", "t-res-le1", tool="listar_agenda", ok=True, n=3)
    await emitir("message_sent", 1, "1:t", "t-res-le1", canal="whatsapp", tamanho=80)
    # mesma leitura bem-sucedida, mas o turno morreu antes de responder
    await emitir("tool_called", 1, "1:t", "t-res-le2", tool="listar_agenda", dias=7)
    await emitir("tool_result", 1, "1:t", "t-res-le2", tool="listar_agenda", ok=True, n=3)

    linha = _linha(await queries.resultados(desde), "agenda", "listar")
    assert linha["tentativas"] == 2
    assert linha["sucessos"] == 2
    assert linha["comprovados"] == 1


async def test_retry_da_mesma_leitura_no_turno_nao_duplica_resultado_comprovado():
    """Sem call_id não dá para distinguir retry de uma segunda operação legítima.
    A unidade conservadora é um resultado por turno/capacidade/operação."""
    desde = datetime.now(UTC)
    for _ in range(2):
        await emitir("tool_result", 1, "1:t", "t-res-retry", tool="listar_agenda", ok=True, n=3)
    await emitir("message_sent", 1, "1:t", "t-res-retry", canal="whatsapp", tamanho=80)

    linha = _linha(await queries.resultados(desde), "agenda", "listar")
    assert linha["sucessos"] == 2, "sucesso operacional continua contando execuções"
    assert linha["comprovados"] == 1, "valor não pode dobrar por retry de telemetria"


async def test_mutacao_que_falhou_nao_vira_resultado():
    desde = datetime.now(UTC)

    await emitir("tool_called", 1, "1:t", "t-res-mut", tool="confirmar_evento")
    await emitir("tool_result", 1, "1:t", "t-res-mut", tool="confirmar_evento",
                 ok=False, destino="google", motivo="rede_indisponivel")
    await emitir("message_sent", 1, "1:t", "t-res-mut", canal="whatsapp", tamanho=40)

    linha = _linha(await queries.resultados(desde), "agenda", "confirmar_criar")
    assert (linha["sucessos"], linha["falhas"], linha["comprovados"]) == (0, 1, 0)
    assert ("rede_indisponivel", 1) in linha["motivos"]
    assert linha["taxa_sucesso"] == 0.0 and linha["denominador"] == 1


async def test_mutacao_comprovada_segmenta_por_dependencia():
    desde = datetime.now(UTC)

    await emitir("tool_called", 1, "1:t", "t-res-ok", tool="confirmar_evento")
    await emitir("tool_result", 1, "1:t", "t-res-ok", tool="confirmar_evento",
                 ok=True, destino="google", novo=True, duracao_min=60)

    linha = _linha(await queries.resultados(desde), "agenda", "confirmar_criar")
    assert linha["comprovados"] == 1
    assert linha["por_dependencia"]["google_calendar"] == 1


async def test_confirmacao_repetida_nao_conta_resultado_novo():
    """"Já criei" prova que a trava contra duplicata funcionou, não que um
    compromisso novo passou a existir."""
    desde = datetime.now(UTC)

    await emitir("tool_called", 1, "1:t", "t-res-rep", tool="confirmar_evento")
    await emitir("tool_result", 1, "1:t", "t-res-rep", tool="confirmar_evento",
                 ok=True, motivo="ja_criado", novo=False)

    linha = _linha(await queries.resultados(desde), "agenda", "confirmar_criar")
    assert linha["sucessos"] == 1
    assert linha["comprovados"] == 0


async def test_turno_concluido_e_mensagem_enviada_nao_sao_resultado():
    """O funil do turno mede processamento. Sem tool de capacidade nenhuma, o
    placar de resultados tem que ficar em zero."""
    desde = datetime.now(UTC)

    await emitir("message_received", 1, "1:t", "t-res-vazio", canal="whatsapp", tamanho=10)
    await emitir("turn_completed", 1, "1:t", "t-res-vazio", ok=True, latencia_ms=900.0)
    await emitir("message_sent", 1, "1:t", "t-res-vazio", canal="whatsapp", tamanho=30)

    r = await queries.resultados(desde)
    assert r["comprovados"] == 0
    assert r["por_operacao"] == []


async def test_legado_criar_evento_continua_no_placar_da_agenda():
    desde = datetime.now(UTC)

    await emitir("tool_called", 1, "1:t", "t-res-leg", tool="criar_evento")
    await emitir("tool_result", 1, "1:t", "t-res-leg", tool="criar_evento", ok=True, evento_id=7)

    linha = _linha(await queries.resultados(desde), "agenda", "criar")
    assert linha["comprovados"] == 1


async def test_fato_de_dominio_nao_duplica_a_tool_que_o_gerou():
    """`reminder_created` acompanha o `tool_result` de `criar_lembrete`: contar
    os dois dobraria a capacidade Lembretes."""
    desde = datetime.now(UTC)

    await emitir("tool_called", 1, "1:t", "t-res-lem", tool="criar_lembrete", quando="amanhã")
    await emitir("tool_result", 1, "1:t", "t-res-lem", tool="criar_lembrete", ok=True)
    await emitir("reminder_created", 1, "1:t", "t-res-lem", reminder_id=1, quando="amanhã")

    linha = _linha(await queries.resultados(desde), "lembretes", "criar")
    assert linha["sucessos"] == 1 and linha["comprovados"] == 1


async def test_operacao_sem_tool_entra_pelo_evento_de_dominio():
    """O documento é guardado pelo adapter, fora do turno — sem tool nenhuma.
    Ainda assim é resultado comprovado da capacidade Documentos."""
    desde = datetime.now(UTC)

    await emitir("document_stored", 1, "1:t", nome="boleto.pdf", mime="application/pdf",
                 tamanho=1024)

    linha = _linha(await queries.resultados(desde), "documentos", "guardar")
    assert linha["comprovados"] == 1


async def test_evento_sem_classificacao_aparece_em_vez_de_sumir():
    desde = datetime.now(UTC)

    await emitir("tool_called", 1, "1:t", "t-res-novo", tool="tool_ainda_sem_taxonomia")
    await emitir("tool_result", 1, "1:t", "t-res-novo", tool="tool_ainda_sem_taxonomia", ok=True)

    r = await queries.resultados(desde)
    assert r["sem_classificacao"] >= 2
    assert r["por_operacao"] == []


async def test_resultados_agregam_por_capacidade():
    desde = datetime.now(UTC)

    await emitir("tool_called", 1, "1:t", "t-res-cap", tool="criar_tarefa")
    await emitir("tool_result", 1, "1:t", "t-res-cap", tool="criar_tarefa", ok=True, task_id=1)
    await emitir("tool_called", 1, "1:t", "t-res-cap", tool="listar_tarefas")
    await emitir("tool_result", 1, "1:t", "t-res-cap", tool="listar_tarefas", ok=True, n=2)
    await emitir("message_sent", 1, "1:t", "t-res-cap", canal="telegram", tamanho=50)

    cap = (await queries.resultados(desde))["por_capacidade"]["tarefas"]
    assert cap["tentativas"] == 2 and cap["sucessos"] == 2 and cap["comprovados"] == 2


# ── cobertura de correlação e proveniência (Observabilidade) ────────────


async def test_cobertura_mede_correlacao_e_releases_observadas():
    desde = datetime.now(UTC)

    await emitir("tool_called", 1, "1:t", "t-cob-1", tool="listar_agenda")
    # evento histórico: sem release e sem event_schema, como os de antes da Q1
    async with Sessao() as s:
        s.add(
            ProductEvent(
                tipo="tool_result", member_id=1, session_id="1:t", turn_id="t-cob-1",
                payload={"tool": "listar_agenda", "ok": True},
            )
        )
        await s.commit()

    c = await queries.cobertura(desde)
    releases = dict(c["releases"])
    assert releases["desconhecida"] >= 1, "evento legado continua contável"
    assert sum(releases.values()) == c["total"]
    assert c["com_turno"] >= 2 and 0 < c["taxa_turno"] <= 1.0
    assert c["ultimo_evento"] is not None


async def test_cobertura_de_turno_ignora_quem_nasce_fora_de_turno():
    """`reminder_fired` não responde a pergunta nenhuma: contá-lo como falha de
    correlação transformaria desenho em bug."""
    desde = datetime.now(UTC)

    await emitir("reminder_fired", 1, "1:t", reminder_id=1)

    c = await queries.cobertura(desde)
    assert c["elegiveis_turno"] == 0
    assert c["taxa_turno"] is None, "sem base, sem porcentagem inventada"


# ── as cinco camadas do painel ──────────────────────────────────────────


async def test_dashboard_monta_as_cinco_camadas_na_ordem():
    html = await gerar_html(dias=7)

    camadas = ["Visão executiva", "Produto e jornadas", "Operação",
               "Observabilidade", "Evaluation"]
    posicoes = [html.find(c) for c in camadas]
    assert all(p > 0 for p in posicoes), dict(zip(camadas, posicoes, strict=True))
    assert posicoes == sorted(posicoes), "as camadas vão do executivo ao diagnóstico"
    assert "<script" not in html and "cdn" not in html.lower()


async def test_dashboard_distingue_turno_de_resultado_e_de_jornada():
    """Os três números que o painel antigo confundia. Se o rótulo não os
    separar, "159 turnos concluídos" volta a ser lido como valor entregue."""
    html = await gerar_html(dias=7)

    assert "turnos concluídos" in html
    assert "resultados comprovados" in html
    assert "jornadas resolvidas" in html
    # e a explicação de que um não implica o outro
    assert "não" in html and "processamento" in html


async def test_dashboard_mostra_resultado_por_capacidade_com_denominador():
    await emitir("tool_called", 1, "1:t", "t-dash-cap", tool="listar_agenda", dias=7)
    await emitir("tool_result", 1, "1:t", "t-dash-cap", tool="listar_agenda", ok=True, n=2)
    await emitir("message_sent", 1, "1:t", "t-dash-cap", canal="whatsapp", tamanho=50)

    html = await gerar_html(dias=1)

    assert "agenda" in html and "listar" in html
    # denominador explícito: taxa sem base declarada é taxa que engana
    assert "comprovados" in html and "denominador" in html.lower()


async def test_dashboard_expoe_release_e_cobertura_na_observabilidade():
    await emitir("tool_called", 1, "1:t", "t-dash-obs", tool="listar_tarefas")

    html = await gerar_html(dias=1)

    assert "Releases observadas" in html
    assert "Cobertura de correlação" in html


async def test_dashboard_sem_base_nao_inventa_numero():
    html = await gerar_html(dias=0)

    assert "Sem base" in html or "Sem dados" in html or "sem base" in html


def test_eval_desatualizado_compara_commit_do_run_com_a_release():
    from mordomo.reporting.dashboard import _eval_desatualizado

    assert _eval_desatualizado("1955f22", "1955f22abcd") is False
    assert _eval_desatualizado("1955f22", "2dbab9d7370") is True
    # sem release descoberta não dá para julgar — e julgar errado é pior
    assert _eval_desatualizado("1955f22", None) is False
    assert _eval_desatualizado("", "2dbab9d7370") is False


async def test_dashboard_html_avisa_quando_eval_e_de_outra_release():
    await emitir("tool_called", 1, "1:t", "t-eval-stale", tool="listar_tarefas")

    html = await gerar_html(dias=1)

    assert "eval desatualizado" in html.lower()


@pytest.mark.parametrize(
    ("commit_eval", "espera_alerta"),
    [("release-antiga", True), ("release-atual", False)],
)
async def test_eval_compara_com_processo_atual_e_nao_com_release_dominante(
    monkeypatch, commit_eval, espera_alerta
):
    from mordomo.reporting import dashboard

    dados = await queries.coletar(0)
    dados["cobertura"]["releases"] = [("release-antiga", 100), ("release-atual", 1)]

    async def coletar_controlado(_dias):
        return dados

    monkeypatch.setattr(queries, "coletar", coletar_controlado)
    monkeypatch.setattr(dashboard, "release_atual", lambda: "release-atual")
    monkeypatch.setattr(
        queries,
        "serie_evals",
        lambda: [
            {
                "nome": "datas_ptbr",
                "acertos": 1,
                "total": 1,
                "acuracia": 1.0,
                "trajetoria": [1.0],
                "quando": "agora",
                "commit": commit_eval,
                "detalhe": "",
            }
        ],
    )

    html = await gerar_html(dias=1)

    alerta = "<span class='aviso'>eval desatualizado</span>" in html.lower()
    assert alerta is espera_alerta


async def test_dashboard_textual_distingue_turno_resultado_e_jornada():
    from mordomo.reporting.dashboard import gerar_texto

    await emitir("tool_result", 1, "1:t", "t-texto-niveis", tool="listar_tarefas", ok=True)
    await emitir("message_sent", 1, "1:t", "t-texto-niveis", canal="whatsapp", tamanho=20)

    texto = await gerar_texto(dias=1)

    assert "turno" in texto.lower()
    assert "resultado" in texto.lower()
    assert "jornada" in texto.lower()
    assert "não prova" in texto.lower() or "diferentes" in texto.lower()


async def test_resumo_respeita_a_janela_superior():
    # `ate` no passado: os eventos recém-emitidos ficam FORA do resumo anterior
    agora = datetime.now(UTC)
    anterior = await queries.resumo(agora - timedelta(days=2), agora - timedelta(days=1))
    assert anterior["turnos"] == 0 and anterior["usd"] == 0.0
