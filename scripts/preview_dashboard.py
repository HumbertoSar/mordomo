"""Preview do dashboard com dados sintéticos — sem tocar banco real.

    uv run python scripts/preview_dashboard.py [--dias 7] [--saida dashboard-preview.html]

Para que existe: ver uma mudança no dashboard em SEGUNDOS, sem o ciclo
commit → deploy → /dashboard no Telegram → baixar. Cria um SQLite descartável
em diretório temporário, semeia ~14 dias de eventos realistas em DUAS janelas
(para os Δ dos KPIs terem o que comparar) e gera o HTML.

A semeadura exercita de propósito os cantos que o dado real nem sempre tem:
falha de tool com motivo, issue que falhou no GitHub, convite rejeitado e
eventos órfãos legados (a quebra da seção Saúde)."""

import argparse
import asyncio
import os
import pathlib
import random
import shutil
import tempfile

# O DATABASE_URL precisa existir ANTES de importar mordomo: config.Settings
# lê o ambiente no import (mesmo padrão do tests/conftest.py).
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="mordomo-preview-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'preview.db').as_posix()}"
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["OPENROUTER_API_KEY"] = ""

from datetime import UTC, datetime, timedelta  # noqa: E402

from mordomo.db.models import ProductEvent  # noqa: E402
from mordomo.db.session import Sessao, criar_tabelas, engine  # noqa: E402
from mordomo.plataforma import preparar  # noqa: E402
from mordomo.reporting.dashboard import gerar_html  # noqa: E402

random.seed(42)  # preview determinístico: mesma semeadura, mesmo HTML


async def semear() -> None:
    await criar_tabelas()
    agora = datetime.now(UTC)
    eventos: list[ProductEvent] = []

    def ev(tipo: str, dias_atras: float, turn: str | None = None,
           member: int | None = 1, **payload) -> None:
        eventos.append(ProductEvent(
            tipo=tipo, member_id=member,
            session_id=f"{member}:t" if member else None, turn_id=turn,
            ts=agora - timedelta(days=dias_atras, minutes=random.randint(0, 600)),
            payload=payload,
        ))

    # ── turnos de conversa, 14 dias, mais movimento na semana recente ─────
    turno = 0
    for dias_atras in range(14):
        n_turnos = random.randint(2, 8) if dias_atras < 7 else random.randint(1, 5)
        for _ in range(n_turnos):
            turno += 1
            t = f"t{turno}"
            m = random.choice([1, 1, 2, 3])
            ev("message_received", dias_atras, t, m)
            destino = random.choice(["lembretes", "lembretes", "agenda", "cofre", "responder"])
            ev("orchestrator_decision", dias_atras, t, m, destino=destino)
            ev("llm_usage", dias_atras, t, m, no="supervisor",
               modelo="anthropic/claude-haiku-4.5",
               input_tokens=random.randint(400, 1200), output_tokens=random.randint(20, 60),
               latencia_ms=random.randint(700, 3500))
            if destino not in ("responder",):
                tool = {"lembretes": "criar_lembrete", "agenda": "criar_evento",
                        "cofre": "guardar_info"}[destino]
                ok = random.random() > 0.15
                ev("tool_called", dias_atras, t, m, tool=tool)
                ev("tool_result", dias_atras, t, m, tool=tool, ok=ok,
                   **({} if ok else {"motivo": "data_nao_entendida"}))
                if ok and destino == "lembretes":
                    ev("reminder_created", dias_atras, t, m)
                ev("llm_usage", dias_atras, t, m, no=destino,
                   modelo="anthropic/claude-sonnet-4.5",
                   input_tokens=random.randint(1200, 4000),
                   output_tokens=random.randint(80, 300),
                   latencia_ms=random.randint(2000, 9000))
            # espera_fila_ms: a maioria não espera; uns poucos pegam fila longa
            # (mensagens da mesma conversa se atropelando) — exercita a linha
            # de espera de fila da seção Observabilidade
            espera = 0 if random.random() > 0.2 else random.randint(1200, 25000)
            ev("turn_completed", dias_atras, t, m, ok=random.random() > 0.03,
               latencia_ms=max(800.0, random.gauss(4200, 2600)) + espera,
               espera_fila_ms=espera)
            ev("message_sent", dias_atras, t, m, canal="telegram", tamanho=120)

    # ── produto: cofre, pedidos (1 issue falha!), curadoria, convites ─────
    for d in (0, 1, 2, 5, 9, 12):
        ev("document_stored", d, nome="doc.pdf", mime="application/pdf")
    for d, cat in ((1, "funcionalidade"), (3, "funcionalidade"), (4, "problema"),
                   (10, "funcionalidade")):
        turno += 1
        ev("feature_requested", d, f"t{turno}", 2, titulo="pedido", categoria=cat)
        ev("feature_issue_created", d, f"t{turno}", 2, ok=d != 4, categoria=cat)
    ev("curation_run", 1, casos_propostos=3, problemas=1, issue_criada=True,
       relatorios_enviados=2)
    ev("curation_run", 8, casos_propostos=1, problemas=0, issue_criada=True,
       relatorios_enviados=2)
    ev("invite_created", 6, papel="adulto")
    ev("invite_used", 6, member=4, papel="adulto")
    ev("invite_rejected", 11, member=None, motivo="codigo_invalido")
    ev("dashboard_sent", 2, dias=30)
    ev("dashboard_sent", 7, dias=7)

    # ── saúde: erro no grafo, desconhecido e órfãos LEGADOS (sem turn_id de
    # tipo que deveria ter — exercita a quebra de diagnóstico da Saúde) ────
    ev("error", 2, "t3", onde="grafo", tentativa=1)
    ev("unknown_user", 4, member=None, canal="telegram")
    ev("message_sent", 10, canal="telegram", tamanho=42)
    ev("tool_called", 10, tool="criar_lembrete")

    async with Sessao() as s:
        s.add_all(eventos)
        await s.commit()


async def _gerar(dias: int, saida: pathlib.Path) -> None:
    await semear()
    saida.write_text(await gerar_html(dias), encoding="utf-8")
    await engine.dispose()  # solta o .db antes de apagar (Windows trava arquivo aberto)


def main() -> None:
    p = argparse.ArgumentParser(description="Dashboard de preview com dados sintéticos.")
    p.add_argument("--dias", type=int, default=7)
    p.add_argument("--saida", default="dashboard-preview.html")
    args = p.parse_args()

    preparar()
    destino = pathlib.Path(args.saida)
    try:
        asyncio.run(_gerar(args.dias, destino))
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"Preview gerado: {destino.resolve()} (banco temporário descartado)")


if __name__ == "__main__":
    main()
