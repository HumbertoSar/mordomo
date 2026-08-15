"""Regressões do runner de evals e do histórico versionado."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "run_evals", Path(__file__).parents[1] / "evals" / "run_evals.py"
)
assert _spec is not None and _spec.loader is not None
run_evals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_evals)


def test_salvar_run_preserva_lf_do_historico(tmp_path, monkeypatch):
    historico = tmp_path / "history.csv"
    monkeypatch.setattr(run_evals, "HISTORICO", historico)
    monkeypatch.setattr(run_evals, "_commit_atual", lambda: "abc123")

    run_evals._salvar_run("roteamento", 2, 2, "modelo")
    run_evals._salvar_run("datas", 3, 3, "determinístico")

    assert b"\r\n" not in historico.read_bytes()
