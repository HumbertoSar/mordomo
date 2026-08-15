"""Runner de evals offline.

  uv run python evals/run_evals.py                     # datas pt-BR (grátis, sem chaves)
  uv run python evals/run_evals.py --com-llm           # + roteamento do supervisor (OpenRouter)
  uv run python evals/run_evals.py --com-llm --salvar  # grava o run em results/history.csv

Fluxo de trabalho (o loop do projeto): mexeu em resolver_data ou no prompt do
supervisor → rode isto → o runner mostra o DELTA contra o último run salvo →
`--salvar` registra. O history.csv é versionado de propósito: é a série
"antes/depois" do portfólio, consultável em vez de espalhada em commits.
Espelho no Langfuse (Datasets/Experiments): evals/experimentos_langfuse.py."""

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
        w = csv.DictWriter(f, fieldnames=_CAMPOS, lineterminator="\n")
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


async def eval_qualidade(modelo_candidato: str | None, modelo_juiz: str) -> tuple[int, int, str] | None:
    """Qualidade da resposta, ponta a ponta e com juiz independente.

    Cada caso roda pelo grafo REAL — supervisor, subagente, tools escrevendo no
    banco de dev via um membro descartável — e o juiz (por padrão de OUTRA
    família de modelos, para não avaliar irmão) dá veredito por critério.

    Razão de existir: decidir com dados a pendência do ADR-006 — os subagentes
    aguentam haiku no lugar de sonnet? `--modelo-agente` troca o candidato."""
    from langchain_core.messages import HumanMessage
    from pydantic import BaseModel, Field

    from mordomo.config import settings

    if not settings.openrouter_api_key:
        print("\n── Eval: qualidade ── PULADO (defina OPENROUTER_API_KEY)")
        return None

    import time

    from langgraph.checkpoint.memory import InMemorySaver
    from sqlalchemy import delete

    import mordomo.agents.agenda as mod_agenda
    import mordomo.agents.lembretes as mod_lembretes
    from mordomo.core.graph import build_graph
    from mordomo.core.llm import chat_model
    from mordomo.db.models import ChannelIdentity, FamilyEvent, Member, ProductEvent, Reminder
    from mordomo.db.session import Sessao

    if modelo_candidato:
        # Antes de qualquer subagente ser construído (eles cacheiam o modelo).
        settings.model_agente = modelo_candidato
        mod_lembretes._agente = None
        mod_agenda._agente = None

    class Veredito(BaseModel):
        """Avaliação de UMA resposta do mordomo."""

        acao_correta: bool = Field(description="fez o que a expectativa pede (criar/perguntar/recusar)?")
        nao_inventa: bool = Field(description="NÃO afirma data, hora ou compromisso que não veio das ferramentas?")
        breve: bool = Field(description="curta, formato WhatsApp, sem markdown pesado nem listas longas?")
        tom_mordomo: bool = Field(description="educado, caloroso, brasileiro?")
        comentario: str = Field(description="uma frase: por que passou ou falhou")

    juiz = chat_model(modelo_juiz).with_structured_output(Veredito)
    dados = json.loads((DATASETS / "qualidade_resposta.json").read_text(encoding="utf-8"))

    # Membro descartável: as tools são reais e escrevem no banco de dev.
    async with Sessao() as s:
        membro = Member(nome="EvalQualidade", papel="adulto")
        s.add(membro)
        await s.flush()
        s.add(ChannelIdentity(member_id=membro.id, canal="telegram", external_id="999002999"))
        await s.commit()
        await s.refresh(membro)

    grafo = build_graph(InMemorySaver())
    aprovados, linhas, latencias = 0, [], []
    try:
        for i, caso in enumerate(dados["casos"]):
            cfg = {
                "configurable": {
                    # Thread NOVA por caso: um caso não contamina o contexto do outro
                    "thread_id": f"evalq-{i}",
                    "member_id": membro.id,
                    "member_nome": "Humberto",
                    "member_papel": "adulto",
                    "session_id": f"{membro.id}:evalq",
                    "turn_id": f"evalq-{i}",
                }
            }
            t0 = time.perf_counter()
            resultado = await grafo.ainvoke(
                {
                    "messages": [HumanMessage(caso["frase"])],
                    "member_id": membro.id,
                    "member_nome": "Humberto",
                    "member_papel": "adulto",
                },
                cfg,
            )
            latencias.append(time.perf_counter() - t0)
            resposta = resultado["messages"][-1].content
            if not isinstance(resposta, str):
                resposta = str(resposta)

            veredito: Veredito = await juiz.ainvoke(
                "Você avalia respostas de um assistente 'mordomo de família' que conversa "
                "por Telegram/WhatsApp, em português brasileiro.\n\n"
                f"MENSAGEM DO USUÁRIO: {caso['frase']}\n"
                f"O QUE SERIA CORRETO: {caso['expectativa']}\n"
                f"RESPOSTA DO MORDOMO: {resposta}\n\n"
                "Dê o veredito critério a critério. Sobre 'nao_inventa': o sistema RESOLVE "
                "expressões relativas — se o usuário disse 'sexta' e a resposta confirma a "
                "data exata dessa sexta (ex.: 14/08), isso veio do resolvedor de datas e NÃO "
                "é invenção. Invenção é afirmar compromisso, lembrete ou horário sem relação "
                "com o pedido ou com o que as ferramentas devolveram."
            )
            ok = all([veredito.acao_correta, veredito.nao_inventa, veredito.breve, veredito.tom_mordomo])
            aprovados += ok
            falhas = [
                nome
                for nome, v in [("ação", veredito.acao_correta), ("inventa", veredito.nao_inventa),
                                ("brevidade", veredito.breve), ("tom", veredito.tom_mordomo)]
                if not v
            ]
            detalhe_caso = "" if ok else f"  [{', '.join(falhas)}] {veredito.comentario[:80]}"
            linhas.append(f"  {'✓' if ok else '✗'} {caso['frase']!r:52}{detalhe_caso}")
    finally:
        async with Sessao() as s:
            for tabela, coluna in [
                (ProductEvent, ProductEvent.member_id),
                (Reminder, Reminder.member_id),
                (FamilyEvent, FamilyEvent.criado_por),
                (ChannelIdentity, ChannelIdentity.member_id),
            ]:
                await s.execute(delete(tabela).where(coluna == membro.id))
            await s.execute(delete(Member).where(Member.id == membro.id))
            await s.commit()

    total = len(dados["casos"])
    p50 = sorted(latencias)[len(latencias) // 2]
    modelo = settings.model_agente
    print(
        f"\n── Eval: qualidade da resposta (agente={modelo}, juiz={modelo_juiz}) ── "
        f"{aprovados}/{total} ({aprovados / total:.0%})  ·  p50 do turno: {p50:.1f}s"
    )
    print("\n".join(linhas))
    _mostrar_delta("qualidade_resposta", aprovados, total)
    return aprovados, total, f"agente={modelo} juiz={modelo_juiz} p50={p50:.1f}s"


def main() -> None:
    from mordomo.plataforma import preparar

    preparar()  # ✓/✗ e ── não cabem no cp1252 do console do Windows

    p = argparse.ArgumentParser()
    p.add_argument("--com-llm", action="store_true", help="roda também o eval de roteamento (chama o LLM)")
    p.add_argument("--salvar", action="store_true", help="registra o run em results/history.csv")
    p.add_argument(
        "--qualidade", action="store_true",
        help="eval de qualidade (LLM-as-judge; roda o grafo REAL — precisa do banco de dev)",
    )
    p.add_argument(
        "--modelo-agente", default=None,
        help="candidato para os subagentes no eval de qualidade (ex.: anthropic/claude-haiku-4.5)",
    )
    p.add_argument(
        "--juiz", default="google/gemini-2.5-flash",
        help="modelo juiz — por padrão de OUTRA família, para não avaliar irmão",
    )
    args = p.parse_args()

    acertos, total, detalhe = eval_datas()
    if args.salvar:
        _salvar_run("datas_ptbr", acertos, total, detalhe)

    if args.com_llm:
        resultado = asyncio.run(eval_roteamento())
        if args.salvar and resultado is not None:
            _salvar_run("roteamento", *resultado)

    if args.qualidade:
        resultado = asyncio.run(eval_qualidade(args.modelo_agente, args.juiz))
        if args.salvar and resultado is not None:
            _salvar_run("qualidade_resposta", *resultado)

    if args.salvar:
        print(f"\nRun registrado em {HISTORICO.relative_to(pathlib.Path.cwd())}")
    print("\nDica: casos ✗ são o backlog — melhore resolver_data/prompt e rode de novo.")
    print("Registre o antes/depois com --salvar: é o material do portfólio.\n")
    sys.exit(0 if acertos > 0 else 1)


if __name__ == "__main__":
    main()
