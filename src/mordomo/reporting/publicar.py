"""Monta a pasta que vai para o ar: a página de arquitetura + o painel fresco.

    uv run python -m mordomo.reporting.publicar --saida publico

Por que existe: o painel dentro de `docs/arquitetura.html` é uma CÓPIA, e cópia
apodrece — em três dias o instantâneo embutido já não parecia com o painel real.
Aqui o dashboard é regerado do banco e injetado na página, de uma vez — e o
ledger do topo (linhas, testes, tipos de evento...) deixa de ser texto digitado
à mão e passa a ser contado na hora, com a data do cálculo carimbada ao lado.

O que sai em `--saida`:
  index.html      — a página, com o painel do dia embutido (arquivo ÚNICO, abre
                    offline: é o que dá para mandar por WhatsApp)
  dashboard.html  — o painel sozinho; servido ao lado, a página prefere ESTE
                    (o iframe busca o irmão antes de cair para a cópia embutida)

Por padrão NÃO escreve em docs/: na VPS isso sujaria o working tree e o próximo
`git pull` abortaria (foi o que aconteceu com o backup.sh). Para atualizar os
arquivos versionados na sua máquina, use --atualizar-docs e commite."""

import argparse
import asyncio
import pathlib
import re

from ..plataforma import preparar
from . import queries
from .dashboard import montar_html

_ABRE_PAINEL = '<script type="text/html" id="dash-src">'
_FECHA_SCRIPT = "</script>"

_RAIZ = pathlib.Path(__file__).resolve().parents[3]
PAGINA = _RAIZ / "docs" / "arquitetura.html"
PAINEL_VERSIONADO = _RAIZ / "docs" / "dashboard.html"

_SRC = _RAIZ / "src" / "mordomo"
_TESTS = _RAIZ / "tests"
_ADRS = _RAIZ / "docs" / "adr"

# tipo do evento é sempre o primeiro argumento string de emitir()/emitir_de() —
# ver a convenção documentada no topo de analytics.py
_RGX_EVENTO = re.compile(r'emitir(?:_de)?\(\s*(?:config,\s*)?"([a-z_]+)"')
_RGX_TESTE = re.compile(r"^(?:async )?def test_", re.MULTILINE)
_RGX_NO_GRAFO = re.compile(r"\.add_node\(")
_RGX_TOOL = re.compile(r"^@tool\s*$", re.MULTILINE)
_RGX_ADAPTER = re.compile(r"^class \w+Adapter:\s*$", re.MULTILINE)


def _fmt_milhar(n: int) -> str:
    """1234 -> '1.234' — separador de milhar à brasileira, como o resto da página."""
    return f"{n:,}".replace(",", ".")


def _ler(caminho: pathlib.Path) -> str:
    return caminho.read_text(encoding="utf-8")


def estatisticas_do_codigo() -> dict:
    """Os fatos do ledger do topo da página, contados na ÁRVORE DE CÓDIGO —
    não no banco. Roda dentro do container em produção: `src/`, `tests/` e
    `docs/adr/` são copiados na imagem (só `.git` fica de fora)."""
    linhas_src = sum(
        len(_ler(arq).splitlines())
        for arq in _SRC.rglob("*.py")
        if "__pycache__" not in arq.parts
    )
    testes = sum(
        len(_RGX_TESTE.findall(_ler(arq))) for arq in _TESTS.glob("test_*.py")
    )
    nos_grafo = len(_RGX_NO_GRAFO.findall(_ler(_SRC / "core" / "graph.py")))
    ferramentas = sum(
        len(_RGX_TOOL.findall(_ler(arq))) for arq in (_SRC / "tools").glob("*.py")
    )
    # Adapter de canal = classe "XAdapter" fora do Protocol base (ChannelAdapter
    # em contract.py não conta — é o contrato, não uma implementação).
    canais = sum(
        len(_RGX_ADAPTER.findall(_ler(arq)))
        for arq in (_SRC / "channels").glob("*.py")
        if arq.name != "contract.py"
    )
    adrs = len(list(_ADRS.glob("*.md")))

    tipos_evento: set[str] = set()
    for arq in _SRC.rglob("*.py"):
        if "__pycache__" not in arq.parts:
            tipos_evento.update(_RGX_EVENTO.findall(_ler(arq)))

    return {
        "linhas_src": linhas_src,
        "testes": testes,
        "nos_grafo": nos_grafo,
        "ferramentas": ferramentas,
        "canais": canais,
        "adrs": adrs,
        "tipos_evento": len(tipos_evento),
    }


def _definir(pagina: str, id_: str, valor: str) -> str:
    """Troca o conteúdo de um elemento marcado id="id_" — igual injetar_dashboard,
    mas por ancoragem de id em vez de bloco inteiro (o ledger tem 8 valores soltos,
    não um blob único)."""
    marca = f'id="{id_}">'
    inicio = pagina.find(marca)
    if inicio < 0:
        raise ValueError(f"Âncora id={id_!r} não encontrada em docs/arquitetura.html")
    corpo = inicio + len(marca)
    fim = pagina.find("<", corpo)
    return pagina[:corpo] + valor + pagina[fim:]


def injetar_estatisticas(pagina: str, stats: dict, custo_por_turno: float | None, data: str) -> str:
    """Preenche o ledger do topo com os números reais e carimba a data do cálculo
    — sem isso, cada fato ali é um número digitado à mão que desatualiza sozinho
    (foi exatamente o que aconteceu: 6.260 linhas escritas, 5.358 de verdade)."""
    custo_fmt = f"~US$ {custo_por_turno:.4f}" if custo_por_turno else "—"
    for id_, valor in [
        ("lg-linhas", _fmt_milhar(stats["linhas_src"])),
        ("lg-testes", str(stats["testes"])),
        ("lg-nos", str(stats["nos_grafo"])),
        ("lg-tools", str(stats["ferramentas"])),
        ("lg-canais", str(stats["canais"])),
        ("lg-adrs", str(stats["adrs"])),
        ("lg-custo", custo_fmt),
        ("lg-eventos", str(stats["tipos_evento"])),
        ("ledger-data", f"dados de {data}"),
    ]:
        pagina = _definir(pagina, id_, valor)
    return pagina


def injetar_dashboard(pagina: str, dashboard: str) -> str:
    """Troca o conteúdo do <script id="dash-src"> pelo painel novo.

    O dashboard é HTML sem <script> nenhum (SVG e CSS inline, por decisão de
    projeto) — por isso dá para embutir num script sem escapar nada. Se um dia
    o painel ganhar JavaScript, este ponto quebra ALTO em vez de gerar página
    corrompida em silêncio."""
    if _FECHA_SCRIPT in dashboard:
        raise ValueError(
            "O dashboard passou a conter '</script>' — embutir assim quebraria a "
            "página. Troque o <script type='text/html'> por outro mecanismo."
        )
    inicio = pagina.find(_ABRE_PAINEL)
    if inicio < 0:
        raise ValueError(f"Âncora {_ABRE_PAINEL!r} não encontrada em docs/arquitetura.html")
    corpo = inicio + len(_ABRE_PAINEL)
    fim = pagina.find(_FECHA_SCRIPT, corpo)
    if fim < 0:
        raise ValueError("O bloco do painel embutido não tem fechamento </script>")
    return pagina[:corpo] + dashboard + pagina[fim:]


async def montar(dias: int, saida: pathlib.Path, atualizar_docs: bool) -> pathlib.Path:
    d = await queries.coletar(dias)
    painel = montar_html(d)

    total_turnos = d["turnos"]["total"]
    custo_por_turno = (d["custo"]["total_usd"] / total_turnos) if total_turnos else None
    data = d["gerado_em"].strftime("%d/%m/%Y")

    pagina = injetar_dashboard(_ler(PAGINA), painel)
    pagina = injetar_estatisticas(pagina, estatisticas_do_codigo(), custo_por_turno, data)

    saida.mkdir(parents=True, exist_ok=True)
    (saida / "dashboard.html").write_text(painel, encoding="utf-8")
    (saida / "index.html").write_text(pagina, encoding="utf-8")

    if atualizar_docs:
        PAINEL_VERSIONADO.write_text(painel, encoding="utf-8")
        PAGINA.write_text(pagina, encoding="utf-8")
    return saida


def main() -> None:
    p = argparse.ArgumentParser(description="Monta a pasta pública (página + painel).")
    p.add_argument("--dias", type=int, default=30)
    p.add_argument("--saida", default="publico")
    p.add_argument(
        "--atualizar-docs",
        action="store_true",
        help="também reescreve docs/dashboard.html e docs/arquitetura.html (uso local, para commitar)",
    )
    args = p.parse_args()

    preparar()
    destino = asyncio.run(montar(args.dias, pathlib.Path(args.saida), args.atualizar_docs))
    print(f"Publicado em: {destino.resolve()} (index.html + dashboard.html)")


if __name__ == "__main__":
    main()
