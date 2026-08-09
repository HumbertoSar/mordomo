"""Runner de evals offline.

  uv run python evals/run_evals.py            # datas pt-BR (grátis, sem chaves)
  uv run python evals/run_evals.py --com-llm  # + roteamento do supervisor (usa OpenRouter)

Fluxo de trabalho (o loop do projeto): mexeu em resolver_data ou no prompt do
supervisor → rode isto → compare a acurácia → registre o antes/depois.
Fase 2: subir estes datasets para o Langfuse (Datasets/Experiments) e rodar
por lá também — aí o histórico de runs fica visível no dashboard."""

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

DATASETS = pathlib.Path(__file__).parent / "datasets"


def eval_datas() -> float:
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
    return acertos / total


async def eval_roteamento() -> float:
    from mordomo.agents.supervisor import PROMPT_SUPERVISOR, Decisao
    from mordomo.config import settings
    from mordomo.core.llm import chat_model

    if not settings.openrouter_api_key:
        print("\n── Eval: roteamento ── PULADO (defina OPENROUTER_API_KEY)")
        return 0.0

    from langchain_core.messages import HumanMessage, SystemMessage

    dados = json.loads((DATASETS / "roteamento.json").read_text(encoding="utf-8"))
    modelo = chat_model(settings.model_supervisor).with_structured_output(Decisao)
    sistema = SystemMessage(PROMPT_SUPERVISOR.format(nome="Humberto", papel="adulto"))

    acertos, linhas = 0, []
    for caso in dados["casos"]:
        decisao = await modelo.ainvoke([sistema, HumanMessage(caso["frase"])])
        ok = decisao.destino == caso["esperado"]
        acertos += ok
        linhas.append(f"  {'✓' if ok else '✗'} {caso['frase']!r:52} esperado={caso['esperado']:10} obtido={decisao.destino}")

    total = len(dados["casos"])
    print(f"\n── Eval: roteamento do supervisor ({settings.model_supervisor}) ── {acertos}/{total} ({acertos / total:.0%})")
    print("\n".join(linhas))
    return acertos / total


def main() -> None:
    from mordomo.plataforma import preparar

    preparar()  # ✓/✗ e ── não cabem no cp1252 do console do Windows

    p = argparse.ArgumentParser()
    p.add_argument("--com-llm", action="store_true", help="roda também o eval de roteamento (chama o LLM)")
    args = p.parse_args()

    acc = eval_datas()
    if args.com_llm:
        asyncio.run(eval_roteamento())

    print("\nDica: casos ✗ são o backlog — melhore resolver_data/prompt e rode de novo.")
    print("Registre o antes/depois: é o material do portfólio.\n")
    sys.exit(0 if acc > 0 else 1)


if __name__ == "__main__":
    main()
