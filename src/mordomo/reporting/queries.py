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
from . import taxonomia
from .precos import PRECO_TEMPLATE_WHATSAPP_USD, custo_usd


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


def _janela(desde: datetime, ate: datetime | None):
    """Condições de período. `ate` existe para a janela ANTERIOR (comparação)."""
    cond = [ProductEvent.ts >= desde]
    if ate is not None:
        cond.append(ProductEvent.ts < ate)
    return cond


async def _eventos(
    desde: datetime, tipos: tuple[str, ...], ate: datetime | None = None
) -> list[ProductEvent]:
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent)
            .where(*_janela(desde, ate), ProductEvent.tipo.in_(tipos))
            .order_by(ProductEvent.ts)
        )
        return list(res.scalars())


async def contagem_por_tipo(desde: datetime, ate: datetime | None = None) -> dict[str, int]:
    """GROUP BY tipo — o panorama mais barato que existe."""
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent.tipo, func.count())
            .where(*_janela(desde, ate))
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

    # Latência POR DIA: é a série que responde "está piorando?" — o agregado
    # do período esconde tendência.
    lat_por_dia: dict[str, list[float]] = defaultdict(list)
    for e in eventos:
        if e.payload:
            lat_por_dia[_dia(e.ts)].append(float(e.payload.get("latencia_ms", 0)))

    # Espera de FILA (lock por thread): a fatia da latência que não é LLM, é
    # mensagem se atropelando na mesma conversa. Eventos antigos não têm o
    # campo — ficam de fora em vez de virar zero e achatar o percentil.
    esperas = [
        float(e.payload["espera_fila_ms"])
        for e in eventos
        if e.payload and e.payload.get("espera_fila_ms") is not None
    ]

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
        "p95_por_dia": {d: percentil(v, 0.95) for d, v in sorted(lat_por_dia.items())},
        "espera_p95_ms": percentil(esperas, 0.95),
        "espera_max_ms": max(esperas) if esperas else None,
        "turnos_que_esperaram": sum(1 for v in esperas if v > 1000),
        "turnos_com_espera_medida": len(esperas),
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
    significa expressão pt-BR que o parser não cobre — vira caso novo no eval.

    Uma busca só: a lista de tool_result que os motivos exigem já contém tudo
    que o GROUP BY de sucesso agregava — agregamos os dois em Python."""
    eventos = await _eventos(desde, ("tool_result",))
    por_tool: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "erro": 0})
    motivos: Counter = Counter()
    for e in eventos:
        p = e.payload or {}
        por_tool[p.get("tool") or "?"]["ok" if p.get("ok") else "erro"] += 1
        if p.get("ok") is False:
            motivos[p.get("motivo", "?")] += 1
    return {"por_tool": dict(por_tool), "motivos": motivos.most_common()}


async def custo(desde: datetime) -> dict:
    """Custo por nó, calculado na LEITURA a partir dos tokens.

    A quebra por nó é a mais interessante do portfólio: mostra quanto custa
    ROTEAR versus quanto custa EXECUTAR."""
    eventos = await _eventos(desde, ("llm_usage",))
    por_no: dict[str, dict] = defaultdict(
        lambda: {"chamadas": 0, "input": 0, "output": 0, "usd": 0.0, "latencias": []}
    )
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
        if p.get("latencia_ms") is not None:  # eventos antigos não têm o campo
            linha["latencias"].append(float(p["latencia_ms"]))

        usd = custo_usd(p.get("modelo"), entrada, saida)
        if usd is None:
            sem_preco.add(p.get("modelo") or "?")
        else:
            linha["usd"] += usd

    # A quebra que decide ONDE otimizar: p50/p95 por nó — supervisor lento é
    # roteamento caro; subagente lento é execução (loop ReAct) cara.
    for linha in por_no.values():
        lat = linha.pop("latencias")
        linha["lat_p50_ms"] = percentil(lat, 0.50)
        linha["lat_p95_ms"] = percentil(lat, 0.95)

    total = sum(v["usd"] for v in por_no.values())
    return {
        "por_no": dict(por_no),
        "total_usd": total,
        "modelos_sem_preco": sorted(sem_preco),
    }


async def lembretes(desde: datetime, contagens: dict[str, int] | None = None) -> dict:
    # `contagens` pré-computado: o coletar() faz UM contagem_por_tipo e
    # distribui — antes eram 4 GROUP BYs idênticos por /dashboard.
    contagens = contagens if contagens is not None else await contagem_por_tipo(desde)
    return {
        "criados": contagens.get("reminder_created", 0),
        "disparados": contagens.get("reminder_fired", 0),
        "proativos_enviados": contagens.get("proactive_sent", 0),
    }


async def jornadas(desde: datetime) -> dict:
    """Desfecho das jornadas iniciadas na janela (análise por coorte).

    O estado é o último fato de ciclo de vida de cada ``journey_id``. Assim uma
    resolução seguida de reabertura volta a contar como aberta. Turno concluído,
    mensagem enviada e tool bem-sucedida não entram: nenhum deles prova que a
    necessidade familiar terminou.
    """
    tipos = (
        "journey_started",
        "journey_resolved",
        "journey_abandoned",
        "journey_reopened",
    )
    eventos = await _eventos(desde, tipos)
    por_id: dict[str, list[ProductEvent]] = defaultdict(list)
    for evento in eventos:
        if evento.journey_id:
            por_id[evento.journey_id].append(evento)

    iniciadas: dict[str, ProductEvent] = {}
    for journey_id, fatos in por_id.items():
        inicio = next((e for e in fatos if e.tipo == "journey_started"), None)
        if inicio is not None:
            iniciadas[journey_id] = inicio

    estados: Counter = Counter()
    reaberturas = 0
    por_tipo: Counter = Counter()
    por_carga: Counter = Counter()
    tempos_resolucao: list[float] = []

    for journey_id, inicio in iniciadas.items():
        fatos = por_id[journey_id]
        ultimo = fatos[-1]
        estado = {
            "journey_resolved": "resolvidas",
            "journey_abandoned": "abandonadas",
        }.get(ultimo.tipo, "abertas")
        estados[estado] += 1
        reaberturas += sum(1 for e in fatos if e.tipo == "journey_reopened")

        payload = inicio.payload or {}
        por_tipo[payload.get("journey_type") or "other"] += 1
        for carga in set(payload.get("loads") or []):
            por_carga[carga] += 1

        if ultimo.tipo == "journey_resolved":
            tempos_resolucao.append(max(0.0, (ultimo.ts - inicio.ts).total_seconds()))

    com_desfecho = estados["resolvidas"] + estados["abandonadas"]
    return {
        "iniciadas": len(iniciadas),
        "resolvidas": estados["resolvidas"],
        "abandonadas": estados["abandonadas"],
        "abertas": estados["abertas"],
        "reaberturas": reaberturas,
        "taxa_resolucao": (
            estados["resolvidas"] / com_desfecho if com_desfecho else None
        ),
        "por_tipo": dict(sorted(por_tipo.items())),
        "por_carga": dict(sorted(por_carga.items())),
        "tempo_resolucao_p50_s": percentil(tempos_resolucao, 0.50),
        "tempo_resolucao_p95_s": percentil(tempos_resolucao, 0.95),
    }


async def resultados(desde: datetime) -> dict:
    """Resultado COMPROVADO por capacidade/operação — a métrica que separa
    "processou" de "entregou".

    Três coisas diferentes que o painel antigo confundia:

      turno concluído   → o processamento técnico terminou;
      resultado comprovado → há evidência determinística de efeito/valor;
      jornada resolvida → a necessidade durável chegou ao desfecho (ver
                          :func:`jornadas`).

    `turn_completed`, `message_sent` e `tool_result(ok=True)` NÃO viram
    resultado sozinhos. O que conta, por operação, está em
    `taxonomia.Operacao.prova`:

      leitura_entregue  — tool de leitura ok=True E resposta enviada no MESMO
                          turno (ler o banco sem responder não ajudou ninguém);
      efeito_persistido — a tool só devolve ok=True depois do commit;
      efeito_externo    — ok=True COM o destino comprovado no payload. É o que
                          exclui o "já criei" do sim repetido: ele prova a
                          trava contra duplicata, não um compromisso novo.

    `tentativas` conta só o que registrou entrada (`tool_called`); por isso a
    taxa de sucesso publica o `denominador` (sucessos + falhas) que usou —
    fatia parcial vira porcentagem mentirosa quando o leitor supõe outra base.
    """
    tipos = ("tool_called", "tool_result", "message_sent", *taxonomia.tipos_de_operacao())
    eventos = await _eventos(desde, tipos)

    # Turnos em que ALGUMA resposta saiu no canal — a prova da leitura.
    turnos_com_resposta = {
        e.turn_id for e in eventos if e.tipo == "message_sent" and e.turn_id
    }

    linhas: dict[tuple[str, str], dict] = {}
    sem_classificacao = 0
    for e in eventos:
        p = e.payload or {}
        projecao = taxonomia.projetar(e.tipo, p)
        if projecao is None:
            # Só o que DEVERIA ter classificação conta como buraco: message_sent
            # é canal, e a ausência dele aqui não é falha de taxonomia.
            if e.tipo in ("tool_called", "tool_result"):
                sem_classificacao += 1
            continue
        if projecao.kind not in ("read", "write"):
            continue

        chave = (projecao.capability, projecao.operation)
        linha = linhas.setdefault(
            chave,
            {
                "capability": projecao.capability,
                "operation": projecao.operation,
                "kind": projecao.kind,
                "prova": projecao.prova,
                "tentativas": 0,
                "sucessos": 0,
                "falhas": 0,
                "comprovados": 0,
                "_comprovados_ids": set(),
                "motivos": Counter(),
                "por_dependencia": Counter(),
            },
        )
        if projecao.evidence == "attempted":
            linha["tentativas"] += 1
            continue
        if projecao.evidence == "failed":
            linha["falhas"] += 1
            linha["motivos"][p.get("motivo") or "?"] += 1
        elif projecao.evidence == "succeeded":
            linha["sucessos"] += 1
            if _comprova(projecao, e, turnos_com_resposta):
                # Sem call_id, a unidade mais honesta é um desfecho por
                # turno/capacidade/operação. Execuções repetidas continuam em
                # `sucessos`, mas retry de telemetria não infla valor entregue.
                identidade = (
                    e.turn_id or f"evento:{e.id}",
                    projecao.capability,
                    projecao.operation,
                )
                linha["_comprovados_ids"].add(identidade)
                linha["comprovados"] = len(linha["_comprovados_ids"])
        if projecao.dependency:
            linha["por_dependencia"][projecao.dependency] += 1

    por_operacao = []
    for linha in sorted(linhas.values(), key=lambda x: (x["capability"], x["operation"])):
        denominador = linha["sucessos"] + linha["falhas"]
        linha.pop("_comprovados_ids", None)
        por_operacao.append(
            {
                **linha,
                "denominador": denominador,
                "taxa_sucesso": (linha["sucessos"] / denominador) if denominador else None,
                "motivos": linha["motivos"].most_common(),
                "por_dependencia": dict(linha["por_dependencia"]),
            }
        )

    por_capacidade: dict[str, dict] = defaultdict(
        lambda: {"tentativas": 0, "sucessos": 0, "falhas": 0, "comprovados": 0}
    )
    for linha in por_operacao:
        alvo = por_capacidade[linha["capability"]]
        for campo in ("tentativas", "sucessos", "falhas", "comprovados"):
            alvo[campo] += linha[campo]

    return {
        "por_operacao": por_operacao,
        "por_capacidade": dict(sorted(por_capacidade.items())),
        "comprovados": sum(linha["comprovados"] for linha in por_operacao),
        "sem_classificacao": sem_classificacao,
    }


def _comprova(projecao, evento, turnos_com_resposta: set[str]) -> bool:
    """A regra de evidência, por tipo de prova. Sem prova declarada na
    taxonomia (preparar, descartar) NUNCA conta: é passo intermediário."""
    if projecao.prova == "leitura_entregue":
        return bool(evento.turn_id) and evento.turn_id in turnos_com_resposta
    if projecao.prova == "efeito_persistido":
        return True
    if projecao.prova == "efeito_externo":
        # Sem destino no payload não há prova de ONDE o efeito aconteceu.
        return projecao.dependency is not None
    return False


async def produto(desde: datetime, contagens: dict[str, int] | None = None) -> dict:
    """As features fora do turno de conversa: cofre, pedidos, curadoria, convites.

    Regra viva do projeto: toda feature nova nasce instrumentada — esta query é
    onde o evento dela passa a aparecer no placar."""
    contagens = contagens if contagens is not None else await contagem_por_tipo(desde)

    pedidos = await _eventos(desde, ("feature_requested",))
    # Eventos v1 (antes do "Pedidos v2") não têm `categoria` — e o v1 só
    # existia para funcionalidade, então esse é o rótulo historicamente correto.
    por_categoria = Counter(
        (e.payload or {}).get("categoria") or "funcionalidade" for e in pedidos
    )

    issues = await _eventos(desde, ("feature_issue_created",))
    issues_ok = sum(1 for e in issues if (e.payload or {}).get("ok"))

    curadorias = await _eventos(desde, ("curation_run",))
    casos_propostos = sum(int((e.payload or {}).get("casos_propostos", 0)) for e in curadorias)

    return {
        "documentos": contagens.get("document_stored", 0),
        "pedidos": len(pedidos),
        "pedidos_por_categoria": por_categoria.most_common(),
        "issues_criadas": issues_ok,
        "issues_falhas": len(issues) - issues_ok,
        "curadorias": len(curadorias),
        "casos_propostos": casos_propostos,
        "convites": {
            "criados": contagens.get("invite_created", 0),
            "usados": contagens.get("invite_used", 0),
            "rejeitados": contagens.get("invite_rejected", 0),
        },
        # /conectar: código gerado no canal antigo × identidade anexada no novo.
        # "usadas" atrás de "criadas" = gente gerou código e não completou a
        # migração — atrito de onboarding que nenhuma outra métrica mostra.
        "conexoes": {
            "criadas": contagens.get("connect_created", 0),
            "usadas": contagens.get("connect_used", 0),
        },
        "dashboards_enviados": contagens.get("dashboard_sent", 0),
    }


async def canais(desde: datetime) -> dict:
    """Adoção por canal + a observabilidade que só o WhatsApp entrega.

    Duas perguntas que este bloco responde e nenhuma outra query respondia:

      1. **A migração está andando?** `canal` viaja em message_received desde
         o dia 1, então a comparação Telegram × WhatsApp sai de graça — é o
         placar da semana de canário.
      2. **A mensagem CHEGOU e foi LIDA?** No Telegram isso não existe: enviar
         era o fim da história. O WhatsApp devolve sent → delivered → read (ou
         failed) por wamid, o que dá taxa de entrega, taxa de leitura e o tempo
         até a leitura — a métrica mais próxima de "a família está usando".

    Um wamid gera VÁRIOS eventos de status (um por etapa); por isso tudo aqui
    conta wamids distintos, não linhas."""
    recebidas = await _eventos(desde, ("message_received",))
    enviadas = await _eventos(desde, ("message_sent",))
    statuses = await _eventos(desde, ("message_status",))
    proativos = await _eventos(desde, ("proactive_channel",))

    por_canal: dict[str, dict] = defaultdict(
        lambda: {"recebidas": 0, "enviadas": 0, "membros": set()}
    )
    por_dia: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in recebidas:
        canal = (e.payload or {}).get("canal") or "?"
        por_canal[canal]["recebidas"] += 1
        por_canal[canal]["membros"].add(e.member_id)
        por_dia[_dia(e.ts)][canal] += 1
    for e in enviadas:
        por_canal[(e.payload or {}).get("canal") or "?"]["enviadas"] += 1

    # wamid → {status: momento (relógio da Meta)}
    etapas: dict[str, dict[str, int | None]] = defaultdict(dict)
    for e in statuses:
        p = e.payload or {}
        if p.get("wamid") and p.get("status"):
            etapas[p["wamid"]][p["status"]] = p.get("ts_canal")

    total = len(etapas)
    entregues = sum(1 for v in etapas.values() if "delivered" in v or "read" in v)
    lidas = sum(1 for v in etapas.values() if "read" in v)
    falhas = sum(1 for v in etapas.values() if "failed" in v)
    # Só quando os DOIS carimbos existem: sem isso a mediana mediria ausência.
    ate_leitura = [
        float(v["read"] - v["sent"])
        for v in etapas.values()
        if isinstance(v.get("read"), int) and isinstance(v.get("sent"), int)
        and v["read"] >= v["sent"]
    ]
    erros = Counter(
        (e.payload or {}).get("erro") or "sem código"
        for e in statuses
        if (e.payload or {}).get("status") == "failed"
    )

    # Custo do canal: a Meta só cobra TEMPLATE, por mensagem ENTREGUE. Envio
    # com wamid conta quando o status delivered/read chegou; envio antigo sem
    # wamid conta como entregue (teto honesto — melhor superestimar do que
    # esconder custo).
    t_eventos = [e for e in proativos if (e.payload or {}).get("modo") == "template"]
    t_enviados = len(t_eventos)
    t_cobrados = 0
    for e in t_eventos:
        w = (e.payload or {}).get("wamid")
        if not w or "delivered" in etapas.get(w, {}) or "read" in etapas.get(w, {}):
            t_cobrados += 1

    return {
        "por_canal": {
            canal: {"recebidas": v["recebidas"], "enviadas": v["enviadas"],
                    "membros": len([m for m in v["membros"] if m])}
            for canal, v in sorted(por_canal.items())
        },
        "recebidas_por_dia": {d: dict(v) for d, v in sorted(por_dia.items())},
        "whatsapp": {
            "com_status": total,
            "entregues": entregues,
            "lidas": lidas,
            "falhas": falhas,
            "taxa_entrega": (entregues / total) if total else None,
            "taxa_leitura": (lidas / entregues) if entregues else None,
            "p50_ate_leitura_s": percentil(ate_leitura, 0.50),
            "p95_ate_leitura_s": percentil(ate_leitura, 0.95),
            "erros": erros.most_common(5),
            # free_form × template: só a de template é cobrada (por mensagem
            # entregue) — esta quebra é a que vira linha de custo.
            "proativos_por_modo": Counter(
                (e.payload or {}).get("modo") or "?" for e in proativos
            ).most_common(),
            "templates_enviados": t_enviados,
            "templates_cobrados": t_cobrados,
            "custo_templates_usd": t_cobrados * PRECO_TEMPLATE_WHATSAPP_USD,
            "preco_template_usd": PRECO_TEMPLATE_WHATSAPP_USD,
        },
    }


async def latencia_por_canal(desde: datetime) -> dict[str, dict]:
    """p50/p95 da latência do turno POR CANAL, casando `message_received` (que
    carrega o canal) com `turn_completed` pelo turn_id.

    É a query que responde: o caminho webhook+fila do WhatsApp adiciona quanto
    sobre o long polling do Telegram? Se o WhatsApp estiver consistentemente
    pior, o problema é infra do canal — não LLM."""
    recebidas = await _eventos(desde, ("message_received",))
    canal_por_turno = {
        e.turn_id: (e.payload or {}).get("canal") or "?"
        for e in recebidas
        if e.turn_id
    }
    por_canal: dict[str, list[float]] = defaultdict(list)
    for e in await _eventos(desde, ("turn_completed",)):
        canal = canal_por_turno.get(e.turn_id)
        if canal and e.payload:
            por_canal[canal].append(float(e.payload.get("latencia_ms", 0)))
    return {
        canal: {
            "turnos": len(v),
            "p50_ms": percentil(v, 0.50),
            "p95_ms": percentil(v, 0.95),
        }
        for canal, v in sorted(por_canal.items())
    }


async def resumo(desde: datetime, ate: datetime | None = None) -> dict:
    """Os totais que os KPIs comparam com o período anterior — só o essencial,
    para não pagar duas vezes o preço de `coletar` inteiro."""
    contagens = await contagem_por_tipo(desde, ate)
    usd = 0.0
    for e in await _eventos(desde, ("llm_usage",), ate):
        p = e.payload or {}
        usd += custo_usd(p.get("modelo"), int(p.get("input_tokens", 0)),
                         int(p.get("output_tokens", 0))) or 0.0
    return {
        "turnos": contagens.get("turn_completed", 0),
        "lembretes": contagens.get("reminder_created", 0),
        "documentos": contagens.get("document_stored", 0),
        "pedidos": contagens.get("feature_requested", 0),
        "usd": usd,
    }


async def saude(desde: datetime, contagens: dict[str, int] | None = None) -> dict:
    contagens = contagens if contagens is not None else await contagem_por_tipo(desde)
    erros = await _eventos(desde, ("error",))
    return {
        "erros_grafo": contagens.get("error", 0),
        "timeouts_llm": sum(
            1 for e in erros if (e.payload or {}).get("motivo") == "timeout_llm"
        ),
        "falhas_de_parse": contagens.get("orchestrator_parse_error", 0),
        "desconhecidos": contagens.get("unknown_user", 0),
        # Reentrega da Meta barrada pelo dedupe: normal em rajada curta; se
        # CRESCER, o webhook está lento (Meta reentrega por 7 dias).
        "reentregas_meta": contagens.get("message_duplicated", 0),
        # Nenhum canal do membro aceitou o proativo — lembrete que ninguém viu.
        "proativos_falhos": contagens.get("proactive_failed", 0),
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


# Tipos que legitimamente nascem SEM turn_id: proativos (não respondem a uma
# pergunta), unknown_user (não há membro) e comandos fora do grafo (/dashboard,
# /convidar, /vincular, documento no cofre, curadoria agendada). Todo evento
# novo emitido fora de um turno PRECISA entrar aqui, senão vira falso órfão.
SEM_TURNO_POR_DESENHO = (
    "reminder_fired",
    "proactive_sent",
    # Nenhum canal do membro aceitou a mensagem proativa (≠ membro sem canal)
    "proactive_failed",
    "unknown_user",
    "document_stored",
    "dashboard_sent",
    "curation_run",
    "invite_created",
    "invite_used",
    "invite_rejected",
    # /conectar: anexar um canal novo ao mesmo membro (migração de canal)
    "connect_created",
    "connect_used",
    # WhatsApp (fase 3): o canal avisa DEPOIS o que aconteceu com a mensagem —
    # o status chega minutos após o turno terminar, sem como amarrá-lo a ele
    # pelo turn_id. É casado pelo wamid no payload.
    "message_status",
    # Qual desenho de proatividade foi usado (free-form dentro da janela de 24h
    # vs. template pago) — job proativo não nasce de pergunta nenhuma.
    "proactive_channel",
    # Webhook reentregue pela Meta (retry de até 7 dias) e barrado pelo dedupe:
    # é fato de CANAL, não de turno — o turno original já aconteceu.
    "message_duplicated",
    # Piloto Google Calendar (ADR-010): comando explícito e callback HTTP. O
    # callback nem chega pelo canal — quem responde é o navegador, e não há
    # turno de conversa a que amarrar.
    "google_connection_started",
    "google_connection_succeeded",
    "google_connection_failed",
    "google_test_event_created",
    "google_test_event_failed",
    "google_disconnected",
)


async def cobertura(desde: datetime) -> dict:
    """A telemetria olhando para si mesma: dá para cruzar o que foi gravado?

    Órfão (o KPI antigo) responde "faltou turn_id?". Isto responde a pergunta
    inteira: quanto do volume dá para amarrar a um turno, a uma sessão, a uma
    pessoa e a uma jornada — e de qual RELEASE cada evento veio, que é o que
    permite dizer "isto começou no deploy X". Evento anterior à Q1 não tem o
    campo e entra como `desconhecida`: histórico não se reescreve.

    A base de `turno` exclui quem nasce fora de turno por desenho (proativos,
    comandos, statuses do canal) — contá-los como falha de correlação
    transformaria decisão de arquitetura em bug permanente."""
    eventos = await _eventos_todos(desde)
    total = len(eventos)
    elegiveis = [e for e in eventos if e.tipo not in SEM_TURNO_POR_DESENHO]

    com_turno = sum(1 for e in elegiveis if e.turn_id)
    com_sessao = sum(1 for e in eventos if e.session_id)
    com_membro = sum(1 for e in eventos if e.member_id)
    com_jornada = sum(1 for e in eventos if e.journey_id)

    releases: Counter = Counter()
    schemas: Counter = Counter()
    for e in eventos:
        p = e.payload or {}
        releases[p.get("release") or "desconhecida"] += 1
        schemas[str(p.get("event_schema") or "1 (legado)")] += 1

    ultimo = max((e.ts for e in eventos), default=None)
    return {
        "total": total,
        "elegiveis_turno": len(elegiveis),
        "com_turno": com_turno,
        "com_sessao": com_sessao,
        "com_membro": com_membro,
        "com_jornada": com_jornada,
        # None, nunca 0%: sem base, porcentagem é invenção.
        "taxa_turno": (com_turno / len(elegiveis)) if elegiveis else None,
        "taxa_sessao": (com_sessao / total) if total else None,
        "taxa_membro": (com_membro / total) if total else None,
        "taxa_jornada": (com_jornada / total) if total else None,
        "releases": releases.most_common(),
        "schemas": schemas.most_common(),
        "ultimo_evento": ultimo,
        "minutos_desde_ultimo": (
            (datetime.now(UTC) - ultimo).total_seconds() / 60 if ultimo else None
        ),
    }


async def _eventos_todos(desde: datetime) -> list[ProductEvent]:
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(*_janela(desde, None)).order_by(ProductEvent.ts)
        )
        return list(res.scalars())


async def orfaos(desde: datetime) -> int:
    """Eventos sem turn_id — a métrica que vigia a própria instrumentação.

    Fora os tipos de `SEM_TURNO_POR_DESENHO`, este número tem que ser ZERO."""
    async with Sessao() as s:
        res = await s.execute(
            select(func.count()).where(
                ProductEvent.ts >= desde,
                ProductEvent.turn_id.is_(None),
                ProductEvent.tipo.not_in(SEM_TURNO_POR_DESENHO),
            )
        )
        return res.scalar_one()


def serie_evals() -> list[dict]:
    """Lê evals/results/history.csv (versionado) e devolve, por eval, o último
    run + a trajetória de acurácia — a seção Evaluation do dashboard nasce daqui.

    Mora aqui (e não no dashboard) porque este módulo é O ponto de acesso a
    dados do reporting — o dashboard só renderiza o que as queries entregam."""
    import csv
    import pathlib

    caminho = pathlib.Path(__file__).resolve().parents[3] / "evals" / "results" / "history.csv"
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8", newline="") as f:
        linhas = list(csv.DictReader(f))
    por_eval: dict[str, list[dict]] = {}
    for linha in linhas:
        por_eval.setdefault(linha["eval"], []).append(linha)
    saida = []
    for nome, runs in por_eval.items():
        ultimo = runs[-1]
        saida.append(
            {
                "nome": nome,
                "acertos": ultimo["acertos"],
                "total": ultimo["total"],
                "acuracia": float(ultimo["acuracia"]),
                "quando": ultimo["ts"][:16].replace("T", " "),
                "commit": ultimo["commit"],
                "detalhe": ultimo.get("detalhe", ""),
                "trajetoria": [float(r["acuracia"]) for r in runs[-8:]],
            }
        )
    return sorted(saida, key=lambda x: x["nome"])


async def orfaos_por_tipo(desde: datetime) -> list[tuple[str, int, str]]:
    """A quebra que transforma o KPI de órfãos em DIAGNÓSTICO: (tipo, n, dia do
    mais recente). Órfão com data recente = bug ativo de instrumentação; com
    data antiga = resto de antes de um conserto, que sai da janela sozinho."""
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent.tipo, func.count(), func.max(ProductEvent.ts))
            .where(
                ProductEvent.ts >= desde,
                ProductEvent.turn_id.is_(None),
                ProductEvent.tipo.not_in(SEM_TURNO_POR_DESENHO),
            )
            .group_by(ProductEvent.tipo)
            .order_by(func.count().desc())
        )
        return [(tipo, n, _dia(ultimo)) for tipo, n, ultimo in res.all()]


async def coletar(dias: int = 30) -> dict:
    """Tudo que o dashboard precisa, numa passada."""
    agora = datetime.now(UTC)
    desde = agora - timedelta(days=dias)
    contagens = await contagem_por_tipo(desde)
    return {
        "dias": dias,
        "desde": desde,
        "gerado_em": datetime.now(_tz()),
        "turnos": await turnos(desde),
        "canais": await canais(desde),
        "latencia_por_canal": await latencia_por_canal(desde),
        "roteamento": await roteamento(desde),
        "ferramentas": await ferramentas(desde),
        "custo": await custo(desde),
        "lembretes": await lembretes(desde, contagens),
        "jornadas": await jornadas(desde),
        "resultados": await resultados(desde),
        "cobertura": await cobertura(desde),
        "produto": await produto(desde, contagens),
        "saude": await saude(desde, contagens),
        "funil": await funil(desde),
        "orfaos": await orfaos(desde),
        "orfaos_detalhe": await orfaos_por_tipo(desde),
        # A MESMA janela, deslocada para trás: é contra ela que os KPIs mostram Δ.
        "anterior": await resumo(agora - timedelta(days=2 * dias), desde),
    }
