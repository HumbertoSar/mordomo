"""Runner de evals offline.

  uv run python evals/run_evals.py                     # datas pt-BR (grátis, sem chaves)
  uv run python evals/run_evals.py --com-llm           # + roteamento do supervisor (OpenRouter)
  uv run python evals/run_evals.py --com-llm --salvar  # grava o run em results/history.csv

Fluxo de trabalho (o loop do projeto): mexeu em resolver_data ou no prompt do
supervisor → rode isto → o runner mostra o DELTA contra o último run salvo →
`--salvar` registra. O history.csv é versionado de propósito: é a série
"antes/depois" do portfólio, consultável em vez de espalhada em commits.
Fase 2+: subir os datasets para o Langfuse (Datasets/Experiments) também."""

import argparse
import asyncio
import csv
import json
import pathlib
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

DATASETS = pathlib.Path(__file__).parent / "datasets"
HISTORICO = pathlib.Path(__file__).parent / "results" / "history.csv"
_CAMPOS = ["ts", "commit", "eval", "acertos", "total", "acuracia", "detalhe"]


# ── Histórico ────────────────────────────────────────────────────────────


def _commit_atual() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return r.stdout.strip()
    except Exception:  # noqa: BLE001 — sem git instalado, o run vale mesmo assim
        return ""


def _ultimo_run(nome: str) -> dict | None:
    if not HISTORICO.exists():
        return None
    with HISTORICO.open(encoding="utf-8", newline="") as f:
        runs = [linha for linha in csv.DictReader(f) if linha["eval"] == nome]
    return runs[-1] if runs else None


def _mostrar_delta(nome: str, acertos: int, total: int) -> None:
    anterior = _ultimo_run(nome)
    if anterior is None:
        print(f"   (primeiro run registrado de '{nome}' — sem baseline para comparar)")
        return
    delta = acertos / total - float(anterior["acuracia"])
    seta = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
    print(
        f"   {seta} delta vs. último run salvo ({anterior['ts'][:16]}, "
        f"{anterior['commit'] or '?'}): {delta:+.1%} "
        f"(era {anterior['acertos']}/{anterior['total']})"
    )


def _salvar_run(nome: str, acertos: int, total: int, detalhe: str) -> None:
    HISTORICO.parent.mkdir(exist_ok=True)
    novo = not HISTORICO.exists()
    with HISTORICO.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CAMPOS)
        if novo:
            w.writeheader()
        w.writerow(
            {
                "ts": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds"),
                "commit": _commit_atual(),
                "eval": nome,
                "acertos": acertos,
                "total": total,
                "acuracia": f"{acertos / total:.4f}",
                "detalhe": detalhe,
            }
        )


# ── Evals ────────────────────────────────────────────────────────────────


def eval_datas() -> tuple[int, int, str]:
    from mordomo.config import settings
    from mordomo.tools.datas import resolver_data

    dados = json.loads((DATASETS / "datas_ptbr.json").read_text(encoding="utf-8"))
    tz = ZoneInfo(settings.tz_familia)
    base = datetime.fromisoformat(dados["base"]).replace(tzinfo=tz)

    acertos, linhas = 0, []
    for caso in dados["casos"]:
        obtido = resolver_data(caso["expressao"], base=base)
        if caso["esperado"] is None:
            ok = obtido is None
            esperado_txt, obtido_txt = "(pedir esclarecimento)", obtido.isoformat() if obtido else "None"
        else:
            esperado = datetime.fromisoformat(caso["esperado"]).replace(tzinfo=tz)
            ok = obtido is not None and (obtido.date(), obtido.hour, obtido.minute) == (
                esperado.date(), esperado.hour, esperado.minute,
            )
            esperado_txt = esperado.strftime("%d/%m %H:%M")
            obtido_txt = obtido.strftime("%d/%m %H:%M") if obtido else "None"
        acertos += ok
        linhas.append(f"  {'✓' if ok else '✗'} [{caso['tipo']:8}] {caso['expressao']!r:38} esperado={esperado_txt}  obtido={obtido_txt}")

    total = len(dados["casos"])
    print(f"\n── Eval: datas pt-BR ── {acertos}/{total} ({acertos / total:.0%})")
    print("\n".join(linhas))
    _mostrar_delta("datas_ptbr", acertos, total)
    return acertos, total, "resolver_data determinístico"


async def eval_roteamento() -> tuple[int, int, str] | None:
    from mordomo.agents.supervisor import PROMPT_SUPERVISOR, Decisao
    from mordomo.config import settings
    from mordomo.core.contexto import janela
    from mordomo.core.llm import chat_model

    if not settings.openrouter_api_key:
        print("\n── Eval: roteamento ── PULADO (defina OPENROUTER_API_KEY)")
        return None

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    dados = json.loads((DATASETS / "roteamento.json").read_text(encoding="utf-8"))
    modelo = chat_model(settings.model_supervisor).with_structured_output(Decisao)
    sistema = SystemMessage(PROMPT_SUPERVISOR.format(nome="Humberto", papel="adulto"))

    acertos, linhas = 0, []
    for caso in dados["casos"]:
        # Casos "multiturno" trazem histórico — e passam pela MESMA janela que o
        # supervisor usa em produção (ADR-007): o eval testa o caminho real.
        mensagens = []
        for m in caso.get("historico", []):
            tipo = HumanMessage if m["papel"] == "usuario" else AIMessage
            mensagens.append(tipo(m["texto"]))
        mensagens.append(HumanMessage(caso["frase"]))
        decisao = await modelo.ainvoke([sistema, *janela(mensagens)])
        ok = decisao.destino == caso["esperado"]
        acertos += ok
        linhas.append(f"  {'✓' if ok else '✗'} {caso['frase']!r:52} esperado={caso['esperado']:10} obtido={decisao.destino}")

    total = len(dados["casos"])
    print(f"\n── Eval: roteamento do supervisor ({settings.model_supervisor}) ── {acertos}/{total} ({acertos / total:.0%})")
    print("\n".join(linhas))
    _mostrar_delta("roteamento", acertos, total)
    return acertos, total, settings.model_supervisor


def main() -> None:
    from mordomo.plataforma import preparar

    preparar()  # ✓/✗ e ── não cabem no cp1252 do console do Windows

    p = argparse.ArgumentParser()
    p.add_argument("--com-llm", action="store_true", help="roda também o eval de roteamento (chama o LLM)")
    p.add_argument("--salvar", action="store_true", help="registra o run em results/history.csv")
    args = p.parse_args()

    acertos, total, detalhe = eval_datas()
    if args.salvar:
        _salvar_run("datas_ptbr", acertos, total, detalhe)

    if args.com_llm:
        resultado = asyncio.run(eval_roteamento())
        if args.salvar and resultado is not None:
            _salvar_run("roteamento", *resultado)

    if args.salvar:
        print(f"\nRun registrado em {HISTORICO.relative_to(pathlib.Path.cwd())}")
    print("\nDica: casos ✗ são o backlog — melhore resolver_data/prompt e rode de novo.")
    print("Registre o antes/depois com --salvar: é o material do portfólio.\n")
    sys.exit(0 if acertos > 0 else 1)


if __name__ == "__main__":
    main()
