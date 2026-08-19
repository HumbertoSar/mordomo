"""Gera `docs/dashboard.html` — a gestão à vista em um arquivo só.

    uv run python -m mordomo.reporting.dashboard --dias 30

Escolhas de projeto:
  - HTML AUTOCONTIDO: gráficos em SVG inline, zero CDN, zero JavaScript. Abre
    offline, roda no CI, sobrevive a link quebrado e pode ser commitado como
    evidência do que o sistema fazia naquela data.
  - Lê só de `reporting.queries` (agregações), nunca de dado bruto de conversa —
    o dashboard mostra números, não o que a família conversou.
"""

import argparse
import asyncio
import html
import pathlib

from ..observability import release_atual
from ..plataforma import preparar
from . import queries

# ── SVG ──────────────────────────────────────────────────────────────────

_PALETA = ["#4f7cff", "#22a06b", "#e8912d", "#c4453f", "#8b5cf6", "#0ea5b7"]


def _barras_verticais(dados: dict[str, float], unidade: str = "") -> str:
    if not dados:
        return "<p class='vazio'>Sem dados no período.</p>"
    larg_barra, gap, altura = 34, 12, 150
    largura = max(len(dados) * (larg_barra + gap), 240)
    teto = max(dados.values()) or 1
    partes = []
    for i, (rotulo, valor) in enumerate(dados.items()):
        x = i * (larg_barra + gap)
        h = max(2, (valor / teto) * (altura - 30))
        y = altura - h - 18
        # <title> = tooltip nativo do SVG, sem JS: o eixo mostra o rótulo
        # truncado, o hover mostra o dado inteiro
        partes.append(
            f'<rect x="{x}" y="{y:.1f}" width="{larg_barra}" height="{h:.1f}" rx="3" fill="{_PALETA[0]}">'
            f"<title>{html.escape(rotulo)}: {valor:g}{unidade}</title></rect>"
            f'<text class="val" x="{x + larg_barra / 2}" y="{y - 4:.1f}">{valor:g}{unidade}</text>'
            f'<text class="eixo" x="{x + larg_barra / 2}" y="{altura - 4}">{html.escape(rotulo[-5:])}</text>'
        )
    # min-width: gráfico largo ROLA dentro de .rolar em vez de comprimir as
    # barras até sumirem no celular
    return (
        f'<svg class="gr" style="min-width:{largura}px" '
        f'viewBox="0 0 {largura} {altura}" role="img">{"".join(partes)}</svg>'
    )


def _barras_horizontais(itens: list[tuple[str, float]], sufixo: str = "") -> str:
    if not itens:
        return "<p class='vazio'>Sem dados no período.</p>"
    teto = max(v for _, v in itens) or 1
    linhas = []
    for i, (rotulo, valor) in enumerate(itens):
        pct = (valor / teto) * 100
        cor = _PALETA[i % len(_PALETA)]
        dica = html.escape(f"{rotulo}: {valor:g}{sufixo}")
        linhas.append(
            f'<div class="hb" title="{dica}">'
            f'<span class="hb-rot">{html.escape(str(rotulo))}</span>'
            f'<span class="hb-trilho"><span class="hb-cheio" style="width:{pct:.1f}%;background:{cor}"></span></span>'
            f'<span class="hb-val">{valor:g}{sufixo}</span>'
            "</div>"
        )
    return "".join(linhas)


def _histograma_latencia(latencias: list[float]) -> str:
    if not latencias:
        return "<p class='vazio'>Sem turnos no período.</p>"
    faixas = [(0, 2000, "< 2s"), (2000, 5000, "2–5s"), (5000, 10000, "5–10s"), (10000, 10**9, "> 10s")]
    contagem = {rot: 0 for _, _, rot in faixas}
    for ms in latencias:
        for lo, hi, rot in faixas:
            if lo <= ms < hi:
                contagem[rot] += 1
                break
    return _barras_horizontais([(r, c) for r, c in contagem.items()], " turnos")


# ── Blocos ───────────────────────────────────────────────────────────────


def _kpi(rotulo: str, valor: str, nota: str = "", alerta: bool = False, delta: str = "") -> str:
    classe = "kpi alerta" if alerta else "kpi"
    return (
        f'<div class="{classe}"><span class="kpi-val">{html.escape(valor)}</span>'
        f'<span class="kpi-rot">{html.escape(rotulo)}</span>'
        + (f'<span class="kpi-nota">{html.escape(nota)}</span>' if nota else "")
        + delta
        + "</div>"
    )


def _delta(atual: float, anterior: float) -> str:
    """Variação vs. a janela anterior, pronta para entrar no KPI.

    Vazio quando não há base (anterior = 0) — melhor omitir do que inventar
    percentual sobre zero. Cor neutra de propósito: custo subir é ruim, uso
    subir é bom; quem lê é que julga."""
    if not anterior:
        return ""
    var = (atual - anterior) / anterior
    if abs(var) < 0.005:
        return '<span class="kpi-delta">= vs. período anterior</span>'
    seta = "▲" if var > 0 else "▼"
    return f'<span class="kpi-delta">{seta} {var:+.0%} vs. período anterior</span>'


def _ms(v: float | None) -> str:
    return "—" if v is None else f"{v / 1000:.1f}s"


def _pct(v: float | None) -> str:
    """Porcentagem — ou travessão. `None` aqui significa "não há base", e 0%
    seria uma afirmação diferente (e falsa)."""
    return f"{v:.0%}" if v is not None else "—"


def _seg(v: float | None) -> str:
    return f"{v:.0f}s" if v is not None and v < 90 else (f"{v / 60:.0f}min" if v else "—")


def _minutos(v: float | None) -> str:
    if v is None:
        return "—"
    if v < 90:
        return f"{v:.0f}min"
    return f"{v / 60:.0f}h" if v < 60 * 48 else f"{v / 1440:.0f}d"


def _destinos(por_dependencia: dict) -> str:
    """"google_calendar 3 · postgres 1" — a segmentação só aparece quando o
    evento PROVOU a dependência; vazio quando não dá para saber."""
    return " · ".join(f"{dep} {n}" for dep, n in sorted(por_dependencia.items())) or "—"


def _mini_trajetoria(valores: list[float]) -> str:
    """Sparkline de acurácia em SVG — a história do eval numa célula de tabela."""
    if not valores:
        return ""
    larg, alt = 12, 26
    barras = []
    for i, v in enumerate(valores):
        h = max(2.0, v * (alt - 4))
        cor = _PALETA[1] if v >= 0.9 else (_PALETA[2] if v >= 0.7 else _PALETA[3])
        atras = len(valores) - 1 - i
        rotulo = "run mais recente" if atras == 0 else f"{atras} run(s) atrás"
        barras.append(
            f'<rect x="{i * (larg + 3)}" y="{alt - h:.1f}" width="{larg}" height="{h:.1f}" rx="2" fill="{cor}">'
            f"<title>{rotulo}: {v:.0%}</title></rect>"
        )
    return (
        f'<svg width="{len(valores) * (larg + 3)}" height="{alt}" '
        f'viewBox="0 0 {len(valores) * (larg + 3)} {alt}">{"".join(barras)}</svg>'
    )


def _eval_desatualizado(commit: str | None, release: str | None) -> bool:
    """O último run salvo é de OUTRO commit que não o rodando agora?

    Comparação por prefixo porque os dois lados abreviam o SHA em tamanhos
    diferentes (7 no history.csv, 12 na release). Sem um dos dois lados a
    resposta é "não sei" — e "não sei" não pode virar alarme falso, então
    devolve False."""
    if not commit or not release:
        return False
    return not (release.startswith(commit) or commit.startswith(release))


# A frase que impede a leitura errada de sempre: "159 turnos" não é "159 coisas
# resolvidas para a família". Fica no topo, junto dos números que ela qualifica.
NOTA_NIVEIS = (
    "Três níveis diferentes, de propósito: TURNO CONCLUÍDO é processamento "
    "técnico que terminou; RESULTADO COMPROVADO exige evidência determinística "
    "de efeito (leitura respondida no mesmo turno, ou gravação confirmada pelo "
    "destino); JORNADA RESOLVIDA é a necessidade durável chegando ao desfecho. "
    "Um não implica o outro — turno concluído sozinho não prova valor nenhum."
)


def montar_html(d: dict) -> str:
    t, c, f = d["turnos"], d["custo"], d["ferramentas"]
    total_turnos = t["total"]
    ant = d.get("anterior") or {}
    res = d.get("resultados") or {}
    cob = d.get("cobertura") or {}
    jornadas = d.get("jornadas") or {}

    membros_ativos = max(t["membros_por_dia"].values(), default=0)
    comprovados = res.get("comprovados", 0)
    frescor = cob.get("minutos_desde_ultimo")
    kpis_exec = "".join(
        [
            _kpi(
                "turnos concluídos", str(total_turnos),
                "processamento, não valor",
                delta=_delta(total_turnos, ant.get("turnos", 0)),
            ),
            _kpi(
                "resultados comprovados", str(comprovados),
                "com evidência de efeito",
            ),
            _kpi(
                "jornadas resolvidas", str(jornadas.get("resolvidas", 0)),
                f"de {jornadas.get('iniciadas', 0)} iniciada(s)",
            ),
            _kpi("pico de membros/dia", str(membros_ativos)),
            _kpi(
                "custo total", f"US$ {c['total_usd']:.4f}",
                delta=_delta(c["total_usd"], ant.get("usd", 0.0)),
            ),
            _kpi(
                "custo por turno",
                f"US$ {(c['total_usd'] / total_turnos):.4f}" if total_turnos else "—",
                delta=_delta(
                    c["total_usd"] / total_turnos,
                    ant.get("usd", 0.0) / ant["turnos"] if ant.get("turnos") else 0.0,
                ) if total_turnos else "",
            ),
            _kpi(
                "telemetria",
                _minutos(frescor),
                "desde o último evento",
                alerta=bool(frescor is not None and frescor > 60 * 24),
            ),
        ]
    )
    kpis_obs = "".join(
        [
            _kpi(
                "eventos órfãos",
                str(d["orfaos"]),
                "sem turn_id — tem que ser 0",
                alerta=d["orfaos"] > 0,
            ),
            _kpi(
                "correlação por turno",
                _pct(cob.get("taxa_turno")),
                f"de {cob.get('elegiveis_turno', 0)} evento(s) elegível(is)",
            ),
            _kpi("correlação por sessão", _pct(cob.get("taxa_sessao"))),
            _kpi("correlação por membro", _pct(cob.get("taxa_membro"))),
            _kpi(
                "correlação por jornada",
                _pct(cob.get("taxa_jornada")),
                "esperada só em necessidades duráveis",
            ),
            _kpi(
                "eventos sem taxonomia",
                str(res.get("sem_classificacao", 0)),
                "tool sem capacidade",
                alerta=bool(res.get("sem_classificacao")),
            ),
            _kpi("eventos no período", str(cob.get("total", 0))),
        ]
    )
    releases_observadas = _barras_horizontais(cob.get("releases") or [])
    schemas_observados = _barras_horizontais(cob.get("schemas") or [])

    # ── resultados por capacidade (o placar de valor) ────────────────────
    por_operacao = res.get("por_operacao") or []
    comprovados_por_capacidade = sorted(
        (
            (cap, v["comprovados"])
            for cap, v in (res.get("por_capacidade") or {}).items()
            if v["comprovados"]
        ),
        key=lambda x: -x[1],
    )
    linhas_resultados = "".join(
        f"<tr><td>{html.escape(r['capability'])}</td><td>{html.escape(r['operation'])}</td>"
        f"<td>{html.escape(r['kind'])}</td>"
        f"<td class='num'>{r['tentativas']}</td><td class='num'>{r['sucessos']}</td>"
        f"<td class='num'>{r['falhas']}</td>"
        f"<td class='num'>{_pct(r['taxa_sucesso'])}"
        f"<span class='den'> /{r['denominador']}</span></td>"
        f"<td class='num'>{r['comprovados']}</td>"
        f"<td class='detalhe'>{html.escape(_destinos(r['por_dependencia']))}</td></tr>"
        for r in por_operacao
    ) or (
        "<tr><td colspan='9' class='vazio'>Sem base: nenhuma capacidade foi "
        "exercitada no período.</td></tr>"
    )

    p95_por_dia = {k: round(v / 1000, 1) for k, v in (t.get("p95_por_dia") or {}).items() if v}

    # Espera de fila: só aparece quando há turnos com o campo medido (eventos
    # de antes da instrumentação não têm) — ausência de dado não vira "0s".
    fila = ""
    if t.get("espera_p95_ms") is not None:
        fila = (
            f"<p class='vazio'>Espera na fila (turno aguardando o anterior da mesma conversa): "
            f"p95 {_ms(t['espera_p95_ms'])} · máx {_ms(t['espera_max_ms'])} · "
            f"{t['turnos_que_esperaram']} de {t['turnos_com_espera_medida']} turno(s) esperaram mais de 1s "
            f"— essa espera já está incluída na latência acima.</p>"
        )

    # ── Canais (a migração Telegram → WhatsApp, medida) ──────────────────
    ca = d.get("canais") or {}
    wa = ca.get("whatsapp") or {}
    por_canal = ca.get("por_canal") or {}

    bloco_jornadas = ""
    if jornadas.get("iniciadas"):
        taxa = jornadas.get("taxa_resolucao")
        kpis_jornadas = "".join(
            [
                _kpi("iniciadas", str(jornadas["iniciadas"])),
                _kpi("abertas", str(jornadas["abertas"])),
                _kpi("resolvidas", str(jornadas["resolvidas"])),
                _kpi("abandonadas", str(jornadas["abandonadas"])),
                _kpi("reaberturas", str(jornadas["reaberturas"]), "retrabalho"),
                _kpi(
                    "taxa de resolução",
                    f"{taxa:.0%}" if taxa is not None else "—",
                    "entre jornadas com desfecho",
                ),
                _kpi(
                    "tempo até resolução",
                    _seg(jornadas.get("tempo_resolucao_p50_s")),
                    "mediana",
                ),
            ]
        )
        bloco_jornadas = f"""
  <h2>Jornadas familiares</h2>
  <div class="kpis">{kpis_jornadas}</div>
  <div class="card" style="margin-top:.75rem">
    <h2 style="margin-top:0">Por tipo</h2>
    {_barras_horizontais(list(jornadas.get("por_tipo", {}).items()))}
    <h2 style="margin-top:1.5rem">Cargas reduzidas</h2>
    {_barras_horizontais(list(jornadas.get("por_carga", {}).items()))}
    <p class="vazio">Resolução exige evento de desfecho; resposta enviada e tool
       bem-sucedida não contam sozinhas.</p>
  </div>"""

    tabela_canais = "".join(
        f"<tr><td>{html.escape(canal)}</td><td class='num'>{v['membros']}</td>"
        f"<td class='num'>{v['recebidas']}</td><td class='num'>{v['enviadas']}</td></tr>"
        for canal, v in por_canal.items()
    )
    # A CURVA DA MIGRAÇÃO: que fatia do que a família falou entrou pelo
    # WhatsApp, dia a dia. É o número que decide quando o Telegram vira dev.
    fatia_wa = {}
    for dia, canais_do_dia in (ca.get("recebidas_por_dia") or {}).items():
        total_dia = sum(canais_do_dia.values())
        if total_dia:
            fatia_wa[dia] = round(100 * canais_do_dia.get("whatsapp", 0) / total_dia)

    membros_wa = (por_canal.get("whatsapp") or {}).get("membros", 0)
    membros_tg = (por_canal.get("telegram") or {}).get("membros", 0)
    kpis_canais = "".join([
        _kpi("membros no WhatsApp", str(membros_wa), f"Telegram: {membros_tg}"),
        _kpi("entrega", _pct(wa.get("taxa_entrega")), f"{wa.get('entregues', 0)} de "
             f"{wa.get('com_status', 0)} mensagens"),
        _kpi("leitura", _pct(wa.get("taxa_leitura")), "das entregues"),
        _kpi("até ser lida", _seg(wa.get("p50_ate_leitura_s")), "mediana"),
        _kpi("falhas de entrega", str(wa.get("falhas", 0)),
             "Meta recusou", alerta=bool(wa.get("falhas"))),
        _kpi("custo do canal", f"US$ {wa.get('custo_templates_usd', 0.0):.4f}",
             f"{wa.get('templates_cobrados', 0)} template(s) — texto livre é grátis"),
    ])
    erros_wa = "".join(
        f"<tr><td>{html.escape(str(codigo))}</td><td class='num'>{n}</td></tr>"
        for codigo, n in (wa.get("erros") or [])
    )
    proativos_modo = _barras_horizontais(
        [(modo, n) for modo, n in (wa.get("proativos_por_modo") or [])]
    )
    canal_ligado = bool(wa.get("com_status") or por_canal.get("whatsapp"))

    # Latência por canal: webhook+fila (WhatsApp) vs. long polling (Telegram).
    # Só aparece com 2+ canais — com um canal só, é a distribuição acima.
    lpc = d.get("latencia_por_canal") or {}
    tabela_lat_canal = ""
    if len(lpc) > 1:
        linhas_lpc = "".join(
            f"<tr><td>{html.escape(canal)}</td><td class='num'>{v['turnos']}</td>"
            f"<td class='num'>{_ms(v['p50_ms'])}</td><td class='num'>{_ms(v['p95_ms'])}</td></tr>"
            for canal, v in lpc.items()
        )
        tabela_lat_canal = (
            '<h2 style="margin-top:1.5rem">Latência por canal</h2>'
            "<div class='rolar'><table>"
            "<tr><th>canal</th><th>turnos</th><th>p50</th><th>p95</th></tr>"
            f"{linhas_lpc}</table></div>"
            "<p class='vazio'>WhatsApp consistentemente acima do Telegram = custo do "
            "caminho webhook+fila, não do LLM.</p>"
        )

    custo_por_no = sorted(
        ((no, round(v["usd"], 5)) for no, v in c["por_no"].items()),
        key=lambda x: -x[1],
    )
    tabela_no = "".join(
        f"<tr><td>{html.escape(no)}</td><td>{v['modelo'] or '—'}</td>"
        f"<td class='num'>{v['chamadas']}</td><td class='num'>{v['input']:,}</td>"
        f"<td class='num'>{v['output']:,}</td><td class='num'>US$ {v['usd']:.5f}</td>"
        f"<td class='num'>{_ms(v.get('lat_p50_ms'))}</td>"
        f"<td class='num'>{_ms(v.get('lat_p95_ms'))}</td></tr>"
        for no, v in sorted(c["por_no"].items(), key=lambda x: -x[1]["usd"])
    )

    tabela_tools = "".join(
        f"<tr><td>{html.escape(tool)}</td><td class='num'>{v['ok']}</td>"
        f"<td class='num'>{v['erro']}</td>"
        f"<td class='num'>{(v['ok'] / (v['ok'] + v['erro'])):.0%}</td></tr>"
        for tool, v in sorted(f["por_tool"].items())
        if (v["ok"] + v["erro"])
    ) or "<tr><td colspan='4' class='vazio'>Nenhuma ferramenta usada no período.</td></tr>"

    motivos = _barras_horizontais([(m, n) for m, n in f["motivos"]]) if f["motivos"] else (
        "<p class='vazio'>Nenhuma falha de ferramenta — bom sinal.</p>"
    )

    aviso_preco = ""
    if c["modelos_sem_preco"]:
        aviso_preco = (
            "<p class='aviso'>Sem preço cadastrado para: "
            + html.escape(", ".join(c["modelos_sem_preco"]))
            + " — o custo acima está SUBESTIMADO. Atualize <code>reporting/precos.py</code>.</p>"
        )

    evals = queries.serie_evals()
    # Frescor é uma propriedade do processo que está executando o painel. A
    # distribuição observada na janela é histórica e pode continuar dominada
    # pela release anterior logo após um deploy.
    release_para_eval = release_atual()

    def status_eval(e: dict) -> str:
        return (
            " <span class='aviso'>eval desatualizado</span>"
            if _eval_desatualizado(e["commit"], release_para_eval)
            else ""
        )

    linhas_evals = "".join(
        f"<tr><td>{html.escape(e['nome'])}{status_eval(e)}</td>"
        f"<td class='num'>{e['acertos']}/{e['total']} ({e['acuracia']:.0%})</td>"
        f"<td>{_mini_trajetoria(e['trajetoria'])}</td>"
        f"<td>{html.escape(e['quando'])}</td><td><code>{html.escape(e['commit'] or '—')}</code></td>"
        f"<td class='detalhe'>{html.escape(e['detalhe'][:60])}</td></tr>"
        for e in evals
    ) or "<tr><td colspan='6' class='vazio'>Nenhum run salvo — rode make evals com --salvar.</td></tr>"

    prod = d["produto"]
    kpis_produto = "".join(
        [
            _kpi(
                "documentos no cofre", str(prod["documentos"]),
                delta=_delta(prod["documentos"], ant.get("documentos", 0)),
            ),
            _kpi(
                "pedidos da família", str(prod["pedidos"]),
                delta=_delta(prod["pedidos"], ant.get("pedidos", 0)),
            ),
            _kpi(
                "issues abertas", str(prod["issues_criadas"]),
                "pedido virou backlog" if prod["issues_criadas"] else "",
                alerta=prod["issues_falhas"] > 0,
            ),
            _kpi("casos de eval propostos", str(prod["casos_propostos"]), "pela curadoria"),
            _kpi("dashboards enviados", str(prod["dashboards_enviados"])),
        ]
    )
    aviso_issues = ""
    if prod["issues_falhas"]:
        aviso_issues = (
            f"<p class='aviso'>{prod['issues_falhas']} pedido(s) NÃO viraram issue "
            "(falha na API do GitHub) — confira o token e os logs.</p>"
        )
    conv = prod["convites"]
    cx = prod["conexoes"]
    # Código de /conectar gerado e não usado = migração que empacou no meio
    # (vale 15 min) — é atrito de onboarding, merece destaque.
    aviso_migracao = ""
    if cx["criadas"] > cx["usadas"]:
        aviso_migracao = (
            f"<p class='aviso'>{cx['criadas'] - cx['usadas']} código(s) de /conectar "
            "sem uso no período — alguém tentou migrar de canal e não completou "
            "(o código expira em 15 min).</p>"
        )

    bloco_produto = f"""
  <h1 class="secao">🎯 Produto e jornadas <span class="secao-sub">valor e desfecho comprovados</span></h1>
  <p class="vazio">{html.escape(NOTA_NIVEIS)}</p>
  <h2>Resultados por capacidade e operação</h2>
  <div class="card">
    <div class="rolar"><table>
      <tr><th>capacidade</th><th>operação</th><th>tipo</th><th>tentativas</th>
          <th>sucessos</th><th>falhas</th><th>taxa / denominador</th>
          <th>comprovados</th><th>dependência</th></tr>
      {linhas_resultados}
    </table></div>
    <h2 style="margin-top:1.5rem">Resultados comprovados por capacidade</h2>
    {_barras_horizontais(comprovados_por_capacidade)}
  </div>
  {bloco_jornadas or "<div class='card'><p class='vazio'>Sem base: nenhuma jornada foi iniciada no período.</p></div>"}
  <h2>Adoção e ativos do produto</h2>
  <div class="kpis">{kpis_produto}</div>
  {aviso_issues}
  <h2>Pedidos por categoria</h2>
  <div class="card">{
      _barras_horizontais([(cat, n) for cat, n in prod["pedidos_por_categoria"]])
      if prod["pedidos_por_categoria"]
      else "<p class='vazio'>Nenhum pedido no período.</p>"
  }</div>
  <h2>Convites e migração de canal</h2>
  <div class="card">
    {_barras_horizontais([
        ("convites criados", conv["criados"]),
        ("convites usados", conv["usados"]),
        ("convites rejeitados", conv["rejeitados"]),
        ("códigos de /conectar", cx["criadas"]),
        ("migrações concluídas", cx["usadas"]),
    ])}
    {aviso_migracao}
    <p class="vazio">Curadoria: {prod["curadorias"]} rodada(s),
       {prod["casos_propostos"]} caso(s) de eval proposto(s).</p>
  </div>"""

    # A quebra que diz SE os órfãos são bug ativo ou resto histórico: a data do
    # mais recente é o veredito (antiga = sai da janela sozinho).
    detalhe_orfaos = ""
    if d.get("orfaos_detalhe"):
        detalhe_orfaos = (
            '<h2 style="margin-top:1.5rem">Eventos órfãos por tipo</h2>'
            + _barras_horizontais(
                [(f"{tipo} (último: {ultimo})", n) for tipo, n, ultimo in d["orfaos_detalhe"]]
            )
            + "<p class='vazio'>Órfão com data recente = bug de instrumentação ativo; "
            "com data antiga = resto de antes de um conserto, sai da janela sozinho.</p>"
        )

    s = d["saude"]
    # charset: sem ele o Safari chuta latin-1 e quebra acento/emoji (visto no
    # iPhone do Humberto). viewport: sem ele o celular renderiza a 980px e dá
    # zoom-out — o arquivo é aberto DIRETO do Telegram, sem <head> de ninguém.
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mordomo — gestão à vista</title>
<style>
  :root {{
    --fundo:#ffffff; --texto:#1a1d21; --suave:#6b7280; --linha:#e5e7eb; --cartao:#f8fafc;
    --alerta:#c4453f;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fundo:#0f1216; --texto:#e6e8eb; --suave:#9aa3ad; --linha:#232a32; --cartao:#161b22; }}
  }}
  :root[data-theme="dark"] {{
    --fundo:#0f1216; --texto:#e6e8eb; --suave:#9aa3ad; --linha:#232a32; --cartao:#161b22;
  }}
  :root[data-theme="light"] {{
    --fundo:#ffffff; --texto:#1a1d21; --suave:#6b7280; --linha:#e5e7eb; --cartao:#f8fafc;
  }}
  body {{ background:var(--fundo); color:var(--texto); margin:0; padding:2rem 1.25rem 4rem;
         font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; margin:0 0 .25rem; letter-spacing:-.02em; }}
  h2 {{ font-size:1rem; margin:2.5rem 0 .75rem; text-transform:uppercase;
        letter-spacing:.08em; color:var(--suave); font-weight:600; }}
  h1.secao {{ font-size:1.25rem; margin:3.5rem 0 1rem; padding-top:1.5rem;
              border-top:2px solid var(--linha); }}
  .secao-sub {{ font-size:.85rem; color:var(--suave); font-weight:400; margin-left:.5rem; }}
  td.detalhe {{ color:var(--suave); font-size:.78rem; }}
  .sub {{ color:var(--suave); margin:0 0 2rem; font-size:.9rem; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:.75rem; }}
  .kpi {{ background:var(--cartao); border:1px solid var(--linha); border-radius:10px;
          padding:.85rem .95rem; display:flex; flex-direction:column; gap:.15rem; }}
  .kpi.alerta {{ border-color:var(--alerta); }}
  .kpi-val {{ font-size:1.5rem; font-weight:650; letter-spacing:-.02em; }}
  .kpi-rot {{ color:var(--suave); font-size:.8rem; }}
  .kpi-nota {{ color:var(--suave); font-size:.7rem; opacity:.8; }}
  .kpi-delta {{ color:var(--suave); font-size:.7rem; font-variant-numeric:tabular-nums; }}
  .card {{ background:var(--cartao); border:1px solid var(--linha); border-radius:10px; padding:1rem 1.1rem; }}
  .gr {{ width:100%; height:auto; max-height:180px; }}
  .gr .val {{ font-size:9px; fill:var(--suave); text-anchor:middle; }}
  .gr .eixo {{ font-size:9px; fill:var(--suave); text-anchor:middle; }}
  .hb {{ display:grid; grid-template-columns:minmax(90px,180px) 1fr auto; gap:.6rem;
         align-items:center; margin:.35rem 0; font-size:.88rem; }}
  .hb-rot {{ color:var(--suave); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .hb-trilho {{ background:var(--linha); border-radius:4px; height:12px; overflow:hidden; }}
  .hb-cheio {{ display:block; height:100%; border-radius:4px; }}
  .hb-val {{ font-variant-numeric:tabular-nums; color:var(--suave); font-size:.82rem; }}
  .rolar {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:.88rem; }}
  th,td {{ text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--linha); }}
  th {{ color:var(--suave); font-weight:600; font-size:.78rem; text-transform:uppercase;
        letter-spacing:.05em; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .vazio {{ color:var(--suave); font-style:italic; font-size:.88rem; }}
  .aviso {{ color:var(--alerta); font-size:.85rem; }}
  .rodape {{ margin-top:3rem; padding-top:1rem; border-top:1px solid var(--linha);
             color:var(--suave); font-size:.8rem; }}
  code {{ background:var(--linha); padding:.1rem .3rem; border-radius:4px; font-size:.85em; }}
  @media (max-width: 520px) {{
    body {{ padding:1rem .75rem 3rem; }}
    h1 {{ font-size:1.2rem; }}
    h1.secao {{ font-size:1.05rem; margin-top:2.5rem; }}
    .kpis {{ grid-template-columns:repeat(2, 1fr); gap:.5rem; }}
    .kpi-val {{ font-size:1.15rem; }}
    .hb {{ grid-template-columns:minmax(64px, 100px) 1fr auto; font-size:.78rem; }}
    .card {{ padding:.75rem .8rem; }}
    table {{ font-size:.78rem; }}
  }}
</style>
<div class="wrap">
  <h1>🤵 Mordomo da Família — gestão à vista</h1>
  <p class="sub">Últimos {d["dias"]} dias · gerado em {d["gerado_em"].strftime("%d/%m/%Y %H:%M")}
     ({d["gerado_em"].tzname()})</p>

  <h1 class="secao">📊 Visão executiva <span class="secao-sub">Analytics para decidir</span></h1>
  <div class="kpis">{kpis_exec}</div>
  <p class="vazio">{html.escape(NOTA_NIVEIS)}</p>

  <h2>Turnos por dia</h2>
  <div class="card rolar">{_barras_verticais({k: v for k, v in d["turnos"]["por_dia"].items()})}</div>

  <h2>Funil do turno</h2>
  <div class="card">{_barras_horizontais([(r, n) for r, n in d["funil"]], " turnos")}</div>

  <h2>Para onde o supervisor roteou</h2>
  <div class="card">{_barras_horizontais(sorted(d["roteamento"].items(), key=lambda x: -x[1]))}</div>

  <h2>Lembretes</h2>
  <div class="card">
    {_barras_horizontais([
        ("criados", d["lembretes"]["criados"]),
        ("disparados", d["lembretes"]["disparados"]),
        ("proativos enviados", d["lembretes"]["proativos_enviados"]),
    ])}
  </div>

  {bloco_produto}

  <h1 class="secao">⚙️ Operação <span class="secao-sub">Canais, execução e dependências</span></h1>
  {f'''
  <div class="kpis">{kpis_canais}</div>

  <h2>Quanto da conversa já entra pelo WhatsApp (% por dia)</h2>
  <div class="card rolar">{_barras_verticais(fatia_wa, "%")}</div>
  ''' if canal_ligado else
  '''<div class="card"><p class="vazio">WhatsApp ainda não recebeu mensagem no
     período — configure as credenciais (docs/whatsapp-fase3.md) e este bloco
     passa a mostrar entrega, leitura e a curva da migração.</p></div>'''}

  <h2>Por canal</h2>
  <div class="card"><table>
    <tr><th>canal</th><th>membros</th><th>recebidas</th><th>enviadas</th></tr>
    {tabela_canais or "<tr><td colspan='4' class='vazio'>Nenhuma mensagem no período.</td></tr>"}
  </table></div>

  {f'''
  <h2>Proatividade no WhatsApp — livre vs. template</h2>
  <div class="card">
    {proativos_modo}
    <p class="vazio">Fora da janela de 24h só sai template aprovado (pago).
       Custo do período: {wa.get("templates_cobrados", 0)} template(s) ×
       US$ {wa.get("preco_template_usd", 0):.4f} =
       <b>US$ {wa.get("custo_templates_usd", 0.0):.4f}</b> — a Meta cobra por
       mensagem ENTREGUE (envio rastreado sem confirmação de entrega não conta;
       envio antigo sem rastreio conta, como teto).</p>
  </div>''' if wa.get("proativos_por_modo") else ""}

  {f'''
  <h2>Entregas recusadas pela Meta</h2>
  <div class="card"><table>
    <tr><th>código do erro</th><th>mensagens</th></tr>{erros_wa}
  </table></div>''' if erros_wa else ""}

  <h1 class="secao">🔬 Observabilidade <span class="secao-sub">como o sistema se comporta</span></h1>
  <div class="kpis">{kpis_obs}</div>

  <h2>Cobertura de correlação</h2>
  <div class="card"><p class="vazio">Turno, sessão e membro permitem reconstrução causal;
     jornada só é esperada para necessidades duráveis.</p></div>
  <h2>Releases observadas</h2>
  <div class="card">{releases_observadas}</div>
  <h2>Contratos de evento</h2>
  <div class="card">{schemas_observados}</div>

  <h2>Latência p95 por dia (s)</h2>
  <div class="card rolar">{_barras_verticais(p95_por_dia, "s")}</div>

  <h2>Distribuição de latência</h2>
  <div class="card">
    {_histograma_latencia(d["turnos"]["latencias_ms"])}
    <p class="vazio">p50 {_ms(d["turnos"]["p50_ms"])} · p95 {_ms(d["turnos"]["p95_ms"])} ·
       máx {_ms(d["turnos"]["max_ms"])}</p>
    {fila}
    {tabela_lat_canal}
  </div>

  <h2>Custo e latência por nó — rotear vs. executar</h2>
  <div class="card">
    {_barras_horizontais(custo_por_no, " USD")}
    <div class="rolar"><table>
      <tr><th>nó</th><th>modelo</th><th>chamadas</th><th>tokens in</th><th>tokens out</th><th>custo</th><th>p50</th><th>p95</th></tr>
      {tabela_no or "<tr><td colspan='8' class='vazio'>Nenhuma chamada de LLM no período.</td></tr>"}
    </table></div>
    {aviso_preco}
  </div>

  <h2>Ferramentas</h2>
  <div class="card">
    <div class="rolar"><table>
      <tr><th>tool</th><th>ok</th><th>erro</th><th>sucesso</th></tr>
      {tabela_tools}
    </table></div>
    <h2 style="margin-top:1.5rem">Motivos de falha</h2>
    {motivos}
  </div>

  <h2>Saúde</h2>
  <div class="card">
    {_barras_horizontais([
        ("erros no grafo", s["erros_grafo"]),
        ("timeouts de LLM (incluídos nos erros acima)", s["timeouts_llm"]),
        ("falhas de parse do roteador", s["falhas_de_parse"]),
        ("mensagens de desconhecidos", s["desconhecidos"]),
        ("proativos sem canal que aceitasse", s["proativos_falhos"]),
        ("webhooks reentregues pela Meta", s["reentregas_meta"]),
    ])}
    {"<p class='aviso'>Reentregas da Meta crescendo = webhook demorando a responder — "
     "a Meta insiste por 7 dias e o dedupe segura, mas a causa é latência no POST.</p>"
     if s["reentregas_meta"] > 5 else ""}
    {detalhe_orfaos}
    <p class="vazio">Para investigar UMA conversa específica (o replay passo a passo),
       use os traces no Langfuse — este painel mostra o agregado.</p>
  </div>

  <h1 class="secao">🧪 Evaluation <span class="secao-sub">a qualidade, medida</span></h1>
  <div class="card">
    <div class="rolar"><table>
      <tr><th>eval</th><th>último resultado</th><th>trajetória</th><th>quando</th><th>commit</th><th>detalhe</th></tr>
      {linhas_evals}
    </table></div>
    <p class="vazio">Fonte: evals/results/history.csv (runs salvos com --salvar).
       Cada barra da trajetória é um run; verde ≥ 90%, laranja ≥ 70%, vermelho abaixo.</p>
  </div>

  <p class="rodape">
    Gerado por <code>mordomo.reporting.dashboard</code> a partir de agregações sobre
    <code>product_events</code> — nenhum conteúdo de conversa aparece aqui.
    Custo calculado na leitura, a partir dos tokens gravados, com a tabela de
    <code>reporting/precos.py</code>.
  </p>
</div>
"""


async def gerar_html(dias: int = 30) -> str:
    """O dashboard como string — é o que o comando /dashboard do chat envia."""
    return montar_html(await queries.coletar(dias))


def montar_texto(d: dict) -> str:
    """A gestão à vista em TEXTO, para canal que não aceita HTML.

    O WhatsApp não recebe `text/html` como documento (a Cloud API rejeita o
    mime), e mandar o painel como anexo quebrado seria pior que não mandar.
    Aqui vai o essencial em texto-primeiro, no tom do canal (regra nº 6) — o
    painel completo continua saindo pelo Telegram e pelo `make dashboard`."""
    t, c = d["turnos"], d["custo"]
    wa = (d.get("canais") or {}).get("whatsapp") or {}
    resultados = d.get("resultados") or {}
    jornadas = d.get("jornadas") or {}
    linhas = [
        f"📊 Gestão à vista — últimos {d['dias']} dias",
        "",
        (f"• turnos concluídos: {t['total']} · "
         f"{max(t['membros_por_dia'].values(), default=0)} pessoa(s) no melhor dia"),
        f"• resultados comprovados: {resultados.get('comprovados', 0)}",
        (f"• jornadas resolvidas: {jornadas.get('resolvidas', 0)} "
         f"de {jornadas.get('iniciadas', 0)} iniciada(s)"),
        "• níveis diferentes: turno concluído não prova resultado nem jornada resolvida",
        f"• latência p50 {_ms(t['p50_ms'])} · p95 {_ms(t['p95_ms'])}",
        f"• taxa de erro {t['taxa_erro']:.0%}",
        f"• custo US$ {c['total_usd']:.4f}"
        + (f" (US$ {c['total_usd'] / t['total']:.4f} por turno)" if t["total"] else ""),
        f"• lembretes criados: {d['lembretes']['criados']}",
    ]
    if jornadas.get("iniciadas"):
        taxa = jornadas.get("taxa_resolucao")
        taxa_txt = f"{taxa:.0%}" if taxa is not None else "sem desfechos ainda"
        linhas.append(
            f"• jornadas: {jornadas['iniciadas']} iniciadas · "
            f"{jornadas['abertas']} abertas · resolução {taxa_txt}"
        )
    if wa.get("com_status"):
        entrega, leitura = wa.get("taxa_entrega"), wa.get("taxa_leitura")
        entregue = f"{entrega:.0%} entregues" if entrega is not None else "entrega sem medida"
        lidas = f", {leitura:.0%} lidas" if leitura is not None else ""
        linhas.append(f"• WhatsApp: {entregue}{lidas}")
    if d.get("orfaos"):
        linhas.append(f"⚠️ {d['orfaos']} evento(s) órfão(s) — instrumentação para conferir")
    linhas.append("")
    linhas.append("O painel completo (gráficos) sai pelo Telegram ou pelo make dashboard.")
    return "\n".join(linhas)


async def gerar_texto(dias: int = 30) -> str:
    return montar_texto(await queries.coletar(dias))


async def _gerar(dias: int, saida: pathlib.Path) -> pathlib.Path:
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(await gerar_html(dias), encoding="utf-8")
    return saida


def main() -> None:
    p = argparse.ArgumentParser(description="Gera o dashboard HTML da gestão à vista.")
    p.add_argument("--dias", type=int, default=30)
    p.add_argument("--saida", default="docs/dashboard.html")
    args = p.parse_args()

    preparar()
    destino = asyncio.run(_gerar(args.dias, pathlib.Path(args.saida)))
    print(f"Dashboard gerado: {destino.resolve()}")


if __name__ == "__main__":
    main()
