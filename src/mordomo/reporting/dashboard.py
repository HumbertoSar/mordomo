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
        partes.append(
            f'<rect x="{x}" y="{y:.1f}" width="{larg_barra}" height="{h:.1f}" rx="3" fill="{_PALETA[0]}"/>'
            f'<text class="val" x="{x + larg_barra / 2}" y="{y - 4:.1f}">{valor:g}{unidade}</text>'
            f'<text class="eixo" x="{x + larg_barra / 2}" y="{altura - 4}">{html.escape(rotulo[-5:])}</text>'
        )
    return f'<svg class="gr" viewBox="0 0 {largura} {altura}" role="img">{"".join(partes)}</svg>'


def _barras_horizontais(itens: list[tuple[str, float]], sufixo: str = "") -> str:
    if not itens:
        return "<p class='vazio'>Sem dados no período.</p>"
    teto = max(v for _, v in itens) or 1
    linhas = []
    for i, (rotulo, valor) in enumerate(itens):
        pct = (valor / teto) * 100
        cor = _PALETA[i % len(_PALETA)]
        linhas.append(
            '<div class="hb">'
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


def _kpi(rotulo: str, valor: str, nota: str = "", alerta: bool = False) -> str:
    classe = "kpi alerta" if alerta else "kpi"
    return (
        f'<div class="{classe}"><span class="kpi-val">{html.escape(valor)}</span>'
        f'<span class="kpi-rot">{html.escape(rotulo)}</span>'
        + (f'<span class="kpi-nota">{html.escape(nota)}</span>' if nota else "")
        + "</div>"
    )


def _ms(v: float | None) -> str:
    return "—" if v is None else f"{v / 1000:.1f}s"


def _serie_evals() -> list[dict]:
    """Lê evals/results/history.csv (versionado) e devolve, por eval, o último
    run + a trajetória de acurácia — a seção Evaluation nasce daqui."""
    import csv

    caminho = pathlib.Path(__file__).resolve().parents[3] / "evals" / "results" / "history.csv"
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8", newline="") as f:
        linhas = list(csv.DictReader(f))
    por_eval: dict[str, list[dict]] = {}
    for linha in linhas:
        por_eval.setdefault(linha["eval"], []).append(linha)
    saida = []
    for nome, runs in por_eval.items():
        ultimo = runs[-1]
        saida.append(
            {
                "nome": nome,
                "acertos": ultimo["acertos"],
                "total": ultimo["total"],
                "acuracia": float(ultimo["acuracia"]),
                "quando": ultimo["ts"][:16].replace("T", " "),
                "commit": ultimo["commit"],
                "detalhe": ultimo.get("detalhe", ""),
                "trajetoria": [float(r["acuracia"]) for r in runs[-8:]],
            }
        )
    return sorted(saida, key=lambda x: x["nome"])


def _mini_trajetoria(valores: list[float]) -> str:
    """Sparkline de acurácia em SVG — a história do eval numa célula de tabela."""
    if not valores:
        return ""
    larg, alt = 12, 26
    barras = []
    for i, v in enumerate(valores):
        h = max(2.0, v * (alt - 4))
        cor = _PALETA[1] if v >= 0.9 else (_PALETA[2] if v >= 0.7 else _PALETA[3])
        barras.append(
            f'<rect x="{i * (larg + 3)}" y="{alt - h:.1f}" width="{larg}" height="{h:.1f}" rx="2" fill="{cor}"/>'
        )
    return (
        f'<svg width="{len(valores) * (larg + 3)}" height="{alt}" '
        f'viewBox="0 0 {len(valores) * (larg + 3)} {alt}">{"".join(barras)}</svg>'
    )


def montar_html(d: dict) -> str:
    t, c, f = d["turnos"], d["custo"], d["ferramentas"]
    total_turnos = t["total"]

    membros_ativos = max(t["membros_por_dia"].values(), default=0)
    kpis_analytics = "".join(
        [
            _kpi("turnos", str(total_turnos), f"últimos {d['dias']} dias"),
            _kpi("pico de membros/dia", str(membros_ativos)),
            _kpi("lembretes criados", str(d["lembretes"]["criados"])),
            _kpi("custo total", f"US$ {c['total_usd']:.4f}"),
            _kpi(
                "custo por turno",
                f"US$ {(c['total_usd'] / total_turnos):.4f}" if total_turnos else "—",
            ),
        ]
    )
    kpis_obs = "".join(
        [
            _kpi("latência p50", _ms(t["p50_ms"])),
            _kpi("latência p95", _ms(t["p95_ms"]), "cauda", alerta=bool(t["p95_ms"] and t["p95_ms"] > 10000)),
            _kpi("taxa de erro", f"{t['taxa_erro']:.0%}", alerta=t["taxa_erro"] > 0.05),
            _kpi(
                "eventos órfãos",
                str(d["orfaos"]),
                "sem turn_id — tem que ser 0",
                alerta=d["orfaos"] > 0,
            ),
        ]
    )

    p95_por_dia = {k: round(v / 1000, 1) for k, v in (t.get("p95_por_dia") or {}).items() if v}

    custo_por_no = sorted(
        ((no, round(v["usd"], 5)) for no, v in c["por_no"].items()),
        key=lambda x: -x[1],
    )
    tabela_no = "".join(
        f"<tr><td>{html.escape(no)}</td><td>{v['modelo'] or '—'}</td>"
        f"<td class='num'>{v['chamadas']}</td><td class='num'>{v['input']:,}</td>"
        f"<td class='num'>{v['output']:,}</td><td class='num'>US$ {v['usd']:.5f}</td></tr>"
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

    evals = _serie_evals()
    linhas_evals = "".join(
        f"<tr><td>{html.escape(e['nome'])}</td>"
        f"<td class='num'>{e['acertos']}/{e['total']} ({e['acuracia']:.0%})</td>"
        f"<td>{_mini_trajetoria(e['trajetoria'])}</td>"
        f"<td>{html.escape(e['quando'])}</td><td><code>{html.escape(e['commit'] or '—')}</code></td>"
        f"<td class='detalhe'>{html.escape(e['detalhe'][:60])}</td></tr>"
        for e in evals
    ) or "<tr><td colspan='6' class='vazio'>Nenhum run salvo — rode make evals com --salvar.</td></tr>"

    s = d["saude"]
    return f"""<title>Mordomo — gestão à vista</title>
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
</style>
<div class="wrap">
  <h1>🤵 Mordomo da Família — gestão à vista</h1>
  <p class="sub">Últimos {d["dias"]} dias · gerado em {d["gerado_em"].strftime("%d/%m/%Y %H:%M")}
     ({d["gerado_em"].tzname()})</p>

  <h1 class="secao">📊 Analytics <span class="secao-sub">o que a família usa</span></h1>
  <div class="kpis">{kpis_analytics}</div>

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

  <h1 class="secao">🔬 Observabilidade <span class="secao-sub">como o sistema se comporta</span></h1>
  <div class="kpis">{kpis_obs}</div>

  <h2>Latência p95 por dia (s)</h2>
  <div class="card rolar">{_barras_verticais(p95_por_dia, "s")}</div>

  <h2>Distribuição de latência</h2>
  <div class="card">
    {_histograma_latencia(d["turnos"]["latencias_ms"])}
    <p class="vazio">p50 {_ms(d["turnos"]["p50_ms"])} · p95 {_ms(d["turnos"]["p95_ms"])} ·
       máx {_ms(d["turnos"]["max_ms"])}</p>
  </div>

  <h2>Custo por nó — rotear vs. executar</h2>
  <div class="card">
    {_barras_horizontais(custo_por_no, " USD")}
    <div class="rolar"><table>
      <tr><th>nó</th><th>modelo</th><th>chamadas</th><th>tokens in</th><th>tokens out</th><th>custo</th></tr>
      {tabela_no or "<tr><td colspan='6' class='vazio'>Nenhuma chamada de LLM no período.</td></tr>"}
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
        ("falhas de parse do roteador", s["falhas_de_parse"]),
        ("mensagens de desconhecidos", s["desconhecidos"]),
    ])}
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
