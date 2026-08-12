"""Datasets e Experiments no Langfuse — a perna de Evals do stack, conectada.

  uv run python evals/experimentos_langfuse.py            # sincroniza datasets + roda experimentos
  uv run python evals/experimentos_langfuse.py --so-sync  # só sobe/atualiza os datasets

Divisão de trabalho com run_evals.py, deliberada:
  - `make evals` continua sendo o loop rápido local (grátis, sem rede no caso
    das datas) e o history.csv continua alimentando o dashboard próprio.
  - ISTO espelha os datasets no Langfuse e registra cada rodada como um
    Experiment (Datasets → Runs na UI): cada caso vira um trace com score,
    e duas rodadas ficam comparáveis lado a lado — o que o CSV não dá.

Itens sobem com id DETERMINÍSTICO (datas-007, rot-012): re-rodar o sync
atualiza o item em vez de duplicar. Caso novo no JSON local → re-rode o sync.

Precisa de LANGFUSE_PUBLIC_KEY/SECRET_KEY no .env; o experimento de roteamento
também precisa de OPENROUTER_API_KEY (sem ela, é pulado — igual ao run_evals)."""

import argparse
import json
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

DATASETS = pathlib.Path(__file__).parent / "datasets"


# ── Cliente ──────────────────────────────────────────────────────────────


def _cliente():
    """Langfuse com as chaves do settings (pydantic lê o .env) — inicialização
    explícita pelo mesmo motivo de observability.py: confiar no os.environ
    faria os experimentos sumirem EM SILÊNCIO."""
    from mordomo.config import settings

    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    from langfuse import Langfuse

    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )


# ── Sync dos datasets locais → Langfuse ──────────────────────────────────


def sincronizar(lf) -> None:
    datas = json.loads((DATASETS / "datas_ptbr.json").read_text(encoding="utf-8"))
    lf.create_dataset(
        name="datas_ptbr",
        description=datas["descricao"],
        metadata={"fonte": "evals/datasets/datas_ptbr.json"},
    )
    for i, caso in enumerate(datas["casos"]):
        lf.create_dataset_item(
            dataset_name="datas_ptbr",
            id=f"datas-{i:03d}",
            input=caso["expressao"],
            expected_output=caso["esperado"],
            # A base viaja NO ITEM: o experimento resolve a expressão contra a
            # mesma segunda-feira de referência do eval local, sempre.
            metadata={"tipo": caso["tipo"], "base": datas["base"]},
        )
    print(f"dataset 'datas_ptbr': {len(datas['casos'])} itens sincronizados")

    rot = json.loads((DATASETS / "roteamento.json").read_text(encoding="utf-8"))
    lf.create_dataset(
        name="roteamento",
        description=rot["descricao"],
        metadata={"fonte": "evals/datasets/roteamento.json"},
    )
    for i, caso in enumerate(rot["casos"]):
        lf.create_dataset_item(
            dataset_name="roteamento",
            id=f"rot-{i:03d}",
            input={"frase": caso["frase"], "historico": caso.get("historico", [])},
            expected_output=caso["esperado"],
            metadata={"tipo": caso.get("tipo", "simples")},
        )
    print(f"dataset 'roteamento': {len(rot['casos'])} itens sincronizados")


# ── Experimentos ─────────────────────────────────────────────────────────


def _acertos(resultado) -> int:
    """Soma os scores 'acerto' de um ExperimentResult (dict ou objeto, o SDK
    já variou entre versões — melhor aceitar os dois que quebrar o resumo)."""
    total = 0
    for r in resultado.item_results:
        for ev in r.evaluations or []:
            v = ev.get("value") if isinstance(ev, dict) else getattr(ev, "value", 0)
            total += int(v or 0)
    return total


def experimento_datas(lf) -> None:
    from mordomo.config import settings
    from mordomo.tools.datas import resolver_data

    tz = ZoneInfo(settings.tz_familia)

    def tarefa(*, item, **_):
        base = datetime.fromisoformat(item.metadata["base"]).replace(tzinfo=tz)
        dt = resolver_data(item.input, base=base)
        return dt.isoformat() if dt else None

    def acerto(*, output, expected_output, **_):
        # Mesmo critério do eval local: data + hora + minuto (None = pedir
        # esclarecimento, e a resposta certa é devolver None mesmo).
        if expected_output is None:
            ok = output is None
        elif output is None:
            ok = False
        else:
            obtido, esperado = datetime.fromisoformat(output), datetime.fromisoformat(expected_output)
            ok = (obtido.date(), obtido.hour, obtido.minute) == (
                esperado.date(), esperado.hour, esperado.minute,
            )
        return {"name": "acerto", "value": float(ok)}

    itens = lf.get_dataset("datas_ptbr").items
    res = lf.run_experiment(
        name="datas_ptbr",
        description="resolver_data determinístico contra a base de referência",
        data=itens,
        task=tarefa,
        evaluators=[acerto],
    )
    print(f"experimento 'datas_ptbr': {_acertos(res)}/{len(itens)} — veja em Datasets → datas_ptbr → Runs")


def experimento_roteamento(lf) -> None:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from mordomo.agents.supervisor import PROMPT_SUPERVISOR, Decisao
    from mordomo.config import settings
    from mordomo.core.contexto import janela
    from mordomo.core.llm import chat_model

    if not settings.openrouter_api_key:
        print("experimento 'roteamento': PULADO (defina OPENROUTER_API_KEY)")
        return

    modelo = chat_model(settings.model_supervisor).with_structured_output(Decisao)
    sistema = SystemMessage(PROMPT_SUPERVISOR.format(nome="Humberto", papel="adulto"))

    async def tarefa(*, item, **_):
        # Mesma janela de contexto da produção (ADR-007) — o experimento testa
        # o caminho real, histórico incluído.
        msgs = [
            HumanMessage(m["texto"]) if m["papel"] == "usuario" else AIMessage(m["texto"])
            for m in item.input.get("historico", [])
        ]
        msgs.append(HumanMessage(item.input["frase"]))
        decisao = await modelo.ainvoke([sistema, *janela(msgs)])
        return decisao.destino

    def acerto(*, output, expected_output, **_):
        return {"name": "acerto", "value": float(output == expected_output)}

    itens = lf.get_dataset("roteamento").items
    res = lf.run_experiment(
        name="roteamento",
        description=f"supervisor structured output ({settings.model_supervisor})",
        data=itens,
        task=tarefa,
        evaluators=[acerto],
        metadata={"modelo": settings.model_supervisor},
        # OpenRouter: concorrência baixa de propósito — 50 chamadas simultâneas
        # é convite para rate limit e cauda de latência (ADR-006).
        max_concurrency=4,
    )
    print(f"experimento 'roteamento': {_acertos(res)}/{len(itens)} — veja em Datasets → roteamento → Runs")


def main() -> None:
    from mordomo.plataforma import preparar

    preparar()
    p = argparse.ArgumentParser(description="Espelha datasets no Langfuse e registra Experiments.")
    p.add_argument("--so-sync", action="store_true", help="só sincroniza os datasets, sem rodar experimentos")
    args = p.parse_args()

    lf = _cliente()
    if lf is None:
        print("LANGFUSE_PUBLIC_KEY/SECRET_KEY ausentes no .env — nada a fazer.")
        return

    sincronizar(lf)
    if not args.so_sync:
        experimento_datas(lf)
        experimento_roteamento(lf)
    lf.flush()


if __name__ == "__main__":
    main()
