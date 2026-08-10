"""Agregações sobre `product_events` — as métricas da gestão à vista.

Divisão de trabalho, deliberada:
  - SQL faz o que SQL faz bem: filtrar por período e agrupar por chave
    (tipo, tool, destino, modelo, membro). Portátil entre Postgres e SQLite
    graças aos acessores JSON do SQLAlchemy (`payload["x"].as_string()`).
  - Python faz percentil e recorte por dia. Funções de data divergem muito
    entre os dois bancos, e na escala de uma família (milhares de linhas, não
    bilhões) trazer as colunas e agrupar em memória é mais simples de ler e
    igualmente rápido. Se um dia isso doer, é sinal de sucesso — aí vira
    materialized view.

Nenhuma função aqui devolve dado bruto de conversa: o dashboard lê agregação."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from ..config import settings
from ..db.models import ProductEvent
from ..db.session import Sessao
from .precos import custo_usd


def _tz():
    return ZoneInfo(settings.tz_familia)


def _dia(ts: datetime) -> str:
    """Dia no fuso da família — 23h de um sábado é sábado, não domingo em UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(_tz()).date().isoformat()


def percentil(valores: list[float], p: float) -> float | None:
    """Percentil por interpolação linear. Sem numpy: são poucas dezenas de pontos."""
    if not valores:
        return None
    ordenados = sorted(valores)
    if len(ordenados) == 1:
        return ordenados[0]
    pos = (len(ordenados) - 1) * p
    baixo = int(pos)
    alto = min(baixo + 1, len(ordenados) - 1)
    return ordenados[baixo] + (ordenados[alto] - ordenados[baixo]) * (pos - baixo)


async def _eventos(desde: datetime, tipos: tuple[str, ...]) -> list[ProductEvent]:
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent)
            .where(ProductEvent.ts >= desde, ProductEvent.tipo.in_(tipos))
            .order_by(ProductEvent.ts)
        )
        return list(res.scalars())


async def contagem_por_tipo(desde: datetime) -> dict[str, int]:
    """GROUP BY tipo — o panorama mais barato que existe."""
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent.tipo, func.count())
            .where(ProductEvent.ts >= desde)
            .group_by(ProductEvent.tipo)
        )
        return {tipo: n for tipo, n in res.all()}


async def turnos(desde: datetime) -> dict:
    """Volume, latência e saúde — tudo derivado de `turn_completed`."""
    eventos = await _eventos(desde, ("turn_completed",))
    latencias = [float(e.payload.get("latencia_ms", 0)) for e in eventos if e.payload]
    por_dia: dict[str, int] = defaultdict(int)
    membros_por_dia: dict[str, set] = defaultdict(set)
    for e in eventos:
        d = _dia(e.ts)
        por_dia[d] += 1
        membros_por_dia[d].add(e.member_id)

    falhos = sum(1 for e in eventos if e.payload and e.payload.get("ok") is False)
    return {
        "total": len(eventos),
        "falhos": falhos,
        "taxa_erro": (falhos / len(eventos)) if eventos else 0.0,
        "p50_ms": percentil(latencias, 0.50),
        "p95_ms": percentil(latencias, 0.95),
        "max_ms": max(latencias) if latencias else None,
        "por_dia": dict(sorted(por_dia.items())),
        "membros_por_dia": {d: len(m) for d, m in sorted(membros_por_dia.items())},
        "latencias_ms": latencias,
    }


async def roteamento(desde: datetime) -> dict[str, int]:
    """Para onde o supervisor mandou. Cruzar com o eval de roteamento é o ponto:
    o eval mede acurácia em casos escolhidos; isto mede o que a família pede."""
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent.payload["destino"].as_string().label("destino"), func.count())
            .where(ProductEvent.ts >= desde, ProductEvent.tipo == "orchestrator_decision")
            # GROUP BY por POSIÇÃO: o Postgres se recusa a agrupar por uma
            # expressão que contém parâmetro ("column payload must appear in the
            # GROUP BY clause"). O ordinal funciona em Postgres e SQLite.
            .group_by(text("1"))
        )
        return {destino or "?": n for destino, n in res.all()}


async def ferramentas(desde: datetime) -> dict:
    """Taxa de sucesso por tool e ranking de motivos de falha.

    O motivo é o achado mais acionável do projeto: `data_nao_entendida` no topo
    significa expressão pt-BR que o parser não cobre — vira caso novo no eval."""
    async with Sessao() as s:
        res = await s.execute(
            select(
                ProductEvent.payload["tool"].as_string().label("tool"),
                ProductEvent.payload["ok"].as_boolean().label("ok"),
                func.count(),
            )
            .where(ProductEvent.ts >= desde, ProductEvent.tipo == "tool_result")
            .group_by(text("1"), text("2"))  # ver nota em roteamento()
        )
        linhas = res.all()

    por_tool: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "erro": 0})
    for tool, ok, n in linhas:
        por_tool[tool or "?"]["ok" if ok else "erro"] += n

    falhas = await _eventos(desde, ("tool_result",))
    motivos = Counter(
        e.payload.get("motivo", "?")
        for e in falhas
        if e.payload and e.payload.get("ok") is False
    )
    return {"por_tool": dict(por_tool), "motivos": motivos.most_common()}


async def custo(desde: datetime) -> dict:
    """Custo por nó e por dia, calculado na LEITURA a partir dos tokens.

    A quebra por nó é a mais interessante do portfólio: mostra quanto custa
    ROTEAR versus quanto custa EXECUTAR."""
    eventos = await _eventos(desde, ("llm_usage",))
    por_no: dict[str, dict] = defaultdict(
        lambda: {"chamadas": 0, "input": 0, "output": 0, "usd": 0.0}
    )
    por_dia: dict[str, float] = defaultdict(float)
    sem_preco: set[str] = set()

    for e in eventos:
        p = e.payload or {}
        no = p.get("no", "?")
        entrada, saida = int(p.get("input_tokens", 0)), int(p.get("output_tokens", 0))
        linha = por_no[no]
        linha["chamadas"] += 1
        linha["input"] += entrada
        linha["output"] += saida
        linha["modelo"] = p.get("modelo")

        usd = custo_usd(p.get("modelo"), entrada, saida)
        if usd is None:
            sem_preco.add(p.get("modelo") or "?")
        else:
            linha["usd"] += usd
            por_dia[_dia(e.ts)] += usd

    total = sum(v["usd"] for v in por_no.values())
    return {
        "por_no": dict(por_no),
        "por_dia": dict(sorted(por_dia.items())),
        "total_usd": total,
        "modelos_sem_preco": sorted(sem_preco),
    }


async def lembretes(desde: datetime) -> dict:
    contagens = await contagem_por_tipo(desde)
    return {
        "criados": contagens.get("reminder_created", 0),
        "disparados": contagens.get("reminder_fired", 0),
        "proativos_enviados": contagens.get("proactive_sent", 0),
    }


async def saude(desde: datetime) -> dict:
    contagens = await contagem_por_tipo(desde)
    return {
        "erros_grafo": contagens.get("error", 0),
        "falhas_de_parse": contagens.get("orchestrator_parse_error", 0),
        "desconhecidos": contagens.get("unknown_user", 0),
    }


async def funil(desde: datetime) -> list[tuple[str, int]]:
    """Quantos TURNOS distintos alcançaram cada etapa.

    Contar turnos e não eventos importa: um turno pode chamar três tools, mas
    ainda é um turno que chegou na etapa 'usou ferramenta'."""
    etapas = [
        ("recebeu mensagem", "message_received"),
        ("roteou", "orchestrator_decision"),
        ("usou ferramenta", "tool_called"),
        ("concluiu o turno", "turn_completed"),
        ("respondeu no canal", "message_sent"),
    ]
    async with Sessao() as s:
        saida = []
        for rotulo, tipo in etapas:
            res = await s.execute(
                select(func.count(func.distinct(ProductEvent.turn_id))).where(
                    ProductEvent.ts >= desde,
                    ProductEvent.tipo == tipo,
                    ProductEvent.turn_id.is_not(None),
                )
            )
            saida.append((rotulo, res.scalar_one()))
        return saida


async def orfaos(desde: datetime) -> int:
    """Eventos sem turn_id — a métrica que vigia a própria instrumentação.

    Fora os proativos (reminder_fired/proactive_sent, que não nascem de uma
    pergunta) e unknown_user, este número tem que ser ZERO."""
    async with Sessao() as s:
        res = await s.execute(
            select(func.count()).where(
                ProductEvent.ts >= desde,
                ProductEvent.turn_id.is_(None),
                ProductEvent.tipo.not_in(
                    ("reminder_fired", "proactive_sent", "unknown_user")
                ),
            )
        )
        return res.scalar_one()


async def coletar(dias: int = 30) -> dict:
    """Tudo que o dashboard precisa, numa passada."""
    desde = datetime.now(UTC) - timedelta(days=dias)
    return {
        "dias": dias,
        "desde": desde,
        "gerado_em": datetime.now(_tz()),
        "turnos": await turnos(desde),
        "roteamento": await roteamento(desde),
        "ferramentas": await ferramentas(desde),
        "custo": await custo(desde),
        "lembretes": await lembretes(desde),
        "saude": await saude(desde),
        "funil": await funil(desde),
        "orfaos": await orfaos(desde),
        "contagens": await contagem_por_tipo(desde),
    }
