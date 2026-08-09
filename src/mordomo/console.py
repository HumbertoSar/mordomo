"""Saída de console em UTF-8 — obrigatório no Windows.

O console do Windows usa cp1252 por padrão: os caracteres de caixa (──), os
✓/✗ dos evals e o 🤵 dos logs estourariam `UnicodeEncodeError`. Todo ponto de
entrada (main, evals, scripts, reporting) chama `forcar_utf8()` antes de imprimir.

Descoberto rodando `evals/run_evals.py` no Windows — o loop deste projeto é
exatamente esse: executar, ver quebrar, consertar."""

import sys


def forcar_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigurar = getattr(stream, "reconfigure", None)
        if reconfigurar is not None:  # TextIOWrapper (Python 3.7+)
            reconfigurar(encoding="utf-8", errors="replace")
