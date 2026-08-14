"""Monta a pasta que vai para o ar: a página de arquitetura + o painel fresco.

    uv run python -m mordomo.reporting.publicar --saida publico

Por que existe: o painel dentro de `docs/arquitetura.html` é uma CÓPIA, e cópia
apodrece — em três dias o instantâneo embutido já não parecia com o painel real.
Aqui o dashboard é regerado do banco e injetado na página, de uma vez.

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

from ..plataforma import preparar
from .dashboard import gerar_html

_ABRE = '<script type="text/html" id="dash-src">'
_FECHA = "</script>"

_RAIZ = pathlib.Path(__file__).resolve().parents[3]
PAGINA = _RAIZ / "docs" / "arquitetura.html"
PAINEL_VERSIONADO = _RAIZ / "docs" / "dashboard.html"


def injetar_dashboard(pagina: str, dashboard: str) -> str:
    """Troca o conteúdo do <script id="dash-src"> pelo painel novo.

    O dashboard é HTML sem <script> nenhum (SVG e CSS inline, por decisão de
    projeto) — por isso dá para embutir num script sem escapar nada. Se um dia
    o painel ganhar JavaScript, este ponto quebra ALTO em vez de gerar página
    corrompida em silêncio."""
    if _FECHA in dashboard:
        raise ValueError(
            "O dashboard passou a conter '</script>' — embutir assim quebraria a "
            "página. Troque o <script type='text/html'> por outro mecanismo."
        )
    inicio = pagina.find(_ABRE)
    if inicio < 0:
        raise ValueError(f"Âncora {_ABRE!r} não encontrada em docs/arquitetura.html")
    corpo = inicio + len(_ABRE)
    fim = pagina.find(_FECHA, corpo)
    if fim < 0:
        raise ValueError("O bloco do painel embutido não tem fechamento </script>")
    return pagina[:corpo] + dashboard + pagina[fim:]


async def montar(dias: int, saida: pathlib.Path, atualizar_docs: bool) -> pathlib.Path:
    painel = await gerar_html(dias)
    pagina = injetar_dashboard(PAGINA.read_text(encoding="utf-8"), painel)

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
