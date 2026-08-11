"""Curadoria automática de evals — o "Dreaming" honesto do mordomo.

Toda semana (e sob demanda via /curadoria), o sistema relê o próprio funil e
transforma falha real em backlog de qualidade:

  1. COLHE, deterministicamente, as expressões de tempo que o parser não
     entendeu em produção (par tool_called/tool_result por turn_id), os motivos
     de falha de ferramenta, os problemas reportados e a saúde geral;
  2. RELATA aos adultos no chat (ritual semanal, curto);
  3. PROPÕE os casos novos de eval numa issue — JSON pronto para colar no
     dataset, com `esperado: PREENCHER`: o que o usuário QUERIA dizer é verdade
     humana, não palpite de LLM.

Nada se autodeploya: o humano preenche o esperado, roda `make evals`, promove.
É a automação do ritual que o CLAUDE.md sempre pediu ("frases reais viram
casos"), não um agente reescrevendo a si mesmo."""

import json
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .analytics import emitir
from .config import settings
from .db.models import Member, ProductEvent
from .db.session import Sessao
from .github_issues import criar_issue
from .notify import notificar
from .reporting.queries import percentil

log = logging.getLogger(__name__)

_FERRAMENTAS_DE_DATA = ("criar_lembrete", "criar_evento")


async def coletar_achados(dias: int = 7) -> dict:
    """Só fatos do funil — nenhum LLM, nenhum conteúdo de conversa além das
    expressões de tempo que o próprio usuário mandou a uma ferramenta."""
    desde = datetime.now(UTC) - timedelta(days=dias)
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(ProductEvent.ts >= desde).order_by(ProductEvent.id)
        )
        eventos = list(res.scalars())

    # Pareamento por turno: a expressão está no tool_called; o veredito, no
    # tool_result seguinte da MESMA ferramenta no MESMO turno.
    expressoes_falhas: list[str] = []
    chamadas: dict[tuple[str, str], str] = {}
    for e in eventos:
        p = e.payload or {}
        if e.tipo == "tool_called" and p.get("tool") in _FERRAMENTAS_DE_DATA and p.get("quando"):
            chamadas[(e.turn_id or "", p["tool"])] = p["quando"]
        if (
            e.tipo == "tool_result"
            and p.get("motivo") == "data_nao_entendida"
            and (expr := chamadas.get((e.turn_id or "", p.get("tool", ""))))
        ):
            expressoes_falhas.append(expr)

    motivos: dict[str, int] = {}
    for e in eventos:
        p = e.payload or {}
        if e.tipo == "tool_result" and p.get("ok") is False:
            motivos[p.get("motivo", "?")] = motivos.get(p.get("motivo", "?"), 0) + 1

    problemas = [
        (e.payload or {}).get("titulo", "?")
        for e in eventos
        if e.tipo == "feature_requested" and (e.payload or {}).get("categoria") == "problema"
    ]
    latencias = [
        float((e.payload or {}).get("latencia_ms", 0))
        for e in eventos
        if e.tipo == "turn_completed"
    ]
    contagem = lambda tipo: sum(1 for e in eventos if e.tipo == tipo)

    return {
        "dias": dias,
        "turnos": contagem("turn_completed"),
        "expressoes_de_data_falhas": sorted(set(expressoes_falhas)),
        "motivos_de_falha": dict(sorted(motivos.items(), key=lambda x: -x[1])),
        "problemas_reportados": problemas,
        "erros_grafo": contagem("error"),
        "falhas_parse": contagem("orchestrator_parse_error"),
        "p95_ms": percentil(latencias, 0.95),
    }


def montar_casos_propostos(achados: dict) -> str:
    """Linhas de dataset prontas para colar — `esperado` fica para o humano."""
    hoje = datetime.now(ZoneInfo(settings.tz_familia)).strftime("%d/%m/%Y")
    linhas = [
        json.dumps(
            {
                "expressao": expr,
                "esperado": "PREENCHER",
                "tipo": "comum",
                "nota": f"caso REAL colhido pela curadoria em {hoje} — falhou em produção",
            },
            ensure_ascii=False,
        )
        for expr in achados["expressoes_de_data_falhas"]
    ]
    return ",\n".join(linhas)


def montar_relatorio(achados: dict) -> str:
    """O resumo semanal, em tom de mordomo e tamanho de chat."""
    a = achados
    p95 = f"{a['p95_ms'] / 1000:.1f}s" if a["p95_ms"] else "—"
    partes = [
        f"🧹 Curadoria semanal do mordomo ({a['dias']} dias, {a['turnos']} turnos, p95 {p95})"
    ]
    if a["expressoes_de_data_falhas"]:
        partes.append(
            f"\n📅 {len(a['expressoes_de_data_falhas'])} expressão(ões) de tempo que eu não entendi:"
        )
        partes += [f"  • \"{e}\"" for e in a["expressoes_de_data_falhas"][:5]]
        partes.append("  → propostas como casos de eval (issue no GitHub)")
    if a["problemas_reportados"]:
        partes.append(f"\n⚠️ {len(a['problemas_reportados'])} problema(s) reportado(s) pela família")
    motivos = {m: n for m, n in a["motivos_de_falha"].items() if m != "data_nao_entendida"}
    if motivos:
        partes.append("\n🔧 Outras falhas de ferramenta: " + ", ".join(f"{m} ({n})" for m, n in motivos.items()))
    if a["erros_grafo"] or a["falhas_parse"]:
        partes.append(f"\n🚨 Erros de sistema: grafo {a['erros_grafo']}, parse {a['falhas_parse']}")
    if len(partes) == 1:
        partes.append("\n✨ Semana limpa: nenhuma falha nova. Sigo a postos.")
    return "\n".join(partes)


async def rodar_curadoria(dias: int = 7) -> dict:
    """Coleta → relata aos adultos → propõe casos em issue (se houver o quê)."""
    achados = await coletar_achados(dias)
    relatorio = montar_relatorio(achados)

    url = None
    if achados["expressoes_de_data_falhas"]:
        casos = montar_casos_propostos(achados)
        corpo = (
            "Casos colhidos do funil de produção — expressões REAIS que o parser "
            "não entendeu. Para promover:\n\n"
            "1. Preencha `esperado` (o que o usuário queria dizer — verdade humana);\n"
            "2. Cole em `evals/datasets/datas_ptbr.json`;\n"
            "3. `make evals` até ✓, e o caso que passar vira teste (catraca).\n\n"
            f"```json\n{casos}\n```"
        )
        url = await criar_issue(
            f"{len(achados['expressoes_de_data_falhas'])} caso(s) de eval propostos "
            f"({datetime.now(ZoneInfo(settings.tz_familia)):%d/%m})",
            corpo,
            "curadoria automática",
            "sistema",
            categoria="curadoria",
        )

    async with Sessao() as s:
        adultos = list(
            (await s.execute(select(Member).where(Member.papel == "adulto"))).scalars()
        )
    enviados = 0
    for adulto in adultos:
        if await notificar(adulto.id, relatorio):
            enviados += 1

    await emitir(
        "curation_run",
        casos_propostos=len(achados["expressoes_de_data_falhas"]),
        problemas=len(achados["problemas_reportados"]),
        issue_criada=url is not None,
        relatorios_enviados=enviados,
    )
    log.info(
        "Curadoria: %d caso(s) proposto(s), issue=%s, %d relatório(s) enviado(s)",
        len(achados["expressoes_de_data_falhas"]), bool(url), enviados,
    )
    return achados
