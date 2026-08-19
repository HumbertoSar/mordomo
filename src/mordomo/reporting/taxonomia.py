"""Taxonomia de capacidades e operações — a projeção que dá sentido ao evento.

O `product_events` guarda FATOS crus (`tool_result` com `tool` e `ok`). Isso
responde "a tool foi bem?" mas não "a capacidade Agenda entregou resultado?".
Este módulo é a ponte, e ele vive na LEITURA de propósito: reescrever as 19
tools para carimbar capacidade/operação em cada payload custaria uma migração,
deixaria o histórico de fora e daria a cada tool a chance de discordar da
taxonomia. Aqui a classificação é uma tabela só, revisável de um lance de olho.

Módulo PURO: sem banco, sem rede, sem I/O. Só stdlib.

As dimensões:
  capability  — o que a família contratou (agenda, tarefas, cofre…)
  operation   — o que foi pedido dentro dela (criar, listar, confirmar…)
  kind        — read | write | delivery | system
  dependency  — de quem o resultado dependeu, quando o evento PROVA
                (google_calendar, postgres, whatsapp, telegram, llm). None
                quando não dá para comprovar: chutar aqui é pior que não saber.
  evidence    — attempted | succeeded | failed | resolved | abandoned
  prova       — o que faz esta operação virar RESULTADO COMPROVADO (ver abaixo);
                None significa "passo intermediário, nunca conta sozinho".

PRIVACIDADE (ADR-005): nada do payload atravessa a projeção. Só saem rótulos
desta tabela — título, e-mail, local, link e token ficam onde estavam.

O QUE PROVA UM RESULTADO (por operação, deliberadamente conservador):
  leitura_entregue — tool de leitura ok=True E resposta efetivamente enviada no
      MESMO turno. Ler o banco e morrer antes de responder não ajudou ninguém.
  efeito_persistido — a tool só devolve ok=True depois do commit; a linha existe.
  efeito_externo — a tool só devolve ok=True depois de o destino externo (ou a
      agenda nativa) confirmar, e o payload diz QUAL foi o destino.
  None — passo intermediário (preparar, descartar): muda estado interno, mas
      não é o valor que a família pediu. Quem fecha é a jornada.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── vocabulário fechado (o que o dashboard pode assumir) ─────────────────

KINDS = ("read", "write", "delivery", "system")
EVIDENCIAS = ("attempted", "succeeded", "failed", "resolved", "abandoned")
PROVAS = ("leitura_entregue", "efeito_persistido", "efeito_externo")


@dataclass(frozen=True)
class Operacao:
    """Uma linha da tabela: o que a operação é, independentemente do evento."""

    capability: str
    operation: str
    kind: str
    dependency: str | None = None
    prova: str | None = None


@dataclass(frozen=True)
class Projecao:
    """Uma operação + o que ESTE evento provou sobre ela."""

    capability: str
    operation: str
    kind: str
    dependency: str | None
    evidence: str
    prova: str | None


# ── tools (as 19 atuais + o legado que ainda vive no histórico) ──────────

_LEITURA = "leitura_entregue"
_PERSISTIDO = "efeito_persistido"
_EXTERNO = "efeito_externo"

TOOLS: dict[str, Operacao] = {
    # Agenda (ADR-010). O destino real — Google ou agenda nativa — só é
    # comprovável pelo payload, então a dependência sai de lá, não daqui.
    "preparar_evento": Operacao("agenda", "preparar_criar", "write"),
    "confirmar_evento": Operacao("agenda", "confirmar_criar", "write", prova=_EXTERNO),
    "descartar_evento": Operacao("agenda", "descartar", "write"),
    "listar_agenda": Operacao("agenda", "listar", "read", prova=_LEITURA),
    "consultar_agenda": Operacao("agenda", "consultar", "read", prova=_LEITURA),
    # Legado: antes de preparar+confirmar existia UMA tool que criava direto.
    # Sem esta linha, todo compromisso criado antes de 17/08/2026 sumiria do
    # placar da capacidade Agenda.
    "criar_evento": Operacao("agenda", "criar", "write", "postgres", _PERSISTIDO),
    # Tarefas
    "criar_tarefa": Operacao("tarefas", "criar", "write", "postgres", _PERSISTIDO),
    "listar_tarefas": Operacao("tarefas", "listar", "read", "postgres", _LEITURA),
    "concluir_tarefa": Operacao("tarefas", "concluir", "write", "postgres", _PERSISTIDO),
    "cancelar_tarefa": Operacao("tarefas", "cancelar", "write", "postgres", _PERSISTIDO),
    "reabrir_tarefa": Operacao("tarefas", "reabrir", "write", "postgres", _PERSISTIDO),
    # Lembretes
    "criar_lembrete": Operacao("lembretes", "criar", "write", "postgres", _PERSISTIDO),
    "listar_lembretes": Operacao("lembretes", "listar", "read", "postgres", _LEITURA),
    "cancelar_lembrete": Operacao("lembretes", "cancelar", "write", "postgres", _PERSISTIDO),
    # Cofre
    "guardar_info": Operacao("cofre", "guardar", "write", "postgres", _PERSISTIDO),
    "buscar_info": Operacao("cofre", "buscar", "read", "postgres", _LEITURA),
    "listar_cofre": Operacao("cofre", "listar", "read", "postgres", _LEITURA),
    "apagar_info": Operacao("cofre", "apagar", "write", "postgres", _PERSISTIDO),
    # Documentos (o cofre de imagens; a guarda acontece FORA do turno, no
    # adapter — por isso `document_stored` também está na tabela de eventos)
    "buscar_documento": Operacao("documentos", "buscar", "read", "postgres", _LEITURA),
    "listar_documentos": Operacao("documentos", "listar", "read", "postgres", _LEITURA),
}


# ── eventos que não passam por tool ──────────────────────────────────────

_EVENTOS: dict[str, tuple[Operacao, str]] = {
    # (operação, evidência que ESTE tipo de evento carrega)
    "document_stored": (
        Operacao("documentos", "guardar", "write", "postgres", _PERSISTIDO), "succeeded"),
    "reminder_created": (
        Operacao("lembretes", "criar", "write", "postgres", _PERSISTIDO), "succeeded"),
    "reminder_fired": (Operacao("lembretes", "disparar", "delivery"), "succeeded"),
    # Fatos de domínio de Tarefas acompanham as tools equivalentes. São
    # classificáveis para cobertura/correlação, mas `coberta_por_tool` impede
    # que sejam somados de novo em resultados.
    "task_created": (
        Operacao("tarefas", "criar", "write", "postgres", _PERSISTIDO), "succeeded"),
    "task_completed": (
        Operacao("tarefas", "concluir", "write", "postgres", _PERSISTIDO), "succeeded"),
    "task_cancelled": (
        Operacao("tarefas", "cancelar", "write", "postgres", _PERSISTIDO), "succeeded"),
    "task_reopened": (
        Operacao("tarefas", "reabrir", "write", "postgres", _PERSISTIDO), "succeeded"),
    # Onboarding: convite (/convidar + /vincular), conexão de canal (/conectar)
    # e o OAuth do Google.
    "invite_created": (Operacao("onboarding", "convidar", "write", "postgres"), "attempted"),
    "invite_used": (
        Operacao("onboarding", "convidar", "write", "postgres", _PERSISTIDO), "succeeded"),
    "invite_rejected": (Operacao("onboarding", "convidar", "write", "postgres"), "failed"),
    "connect_created": (Operacao("onboarding", "conectar", "write", "postgres"), "attempted"),
    "connect_used": (
        Operacao("onboarding", "conectar", "write", "postgres", _PERSISTIDO), "succeeded"),
    "google_connection_started": (
        Operacao("onboarding", "google_oauth", "write", "google_calendar"), "attempted"),
    "google_connection_succeeded": (
        Operacao("onboarding", "google_oauth", "write", "google_calendar", _EXTERNO),
        "succeeded"),
    "google_connection_failed": (
        Operacao("onboarding", "google_oauth", "write", "google_calendar"), "failed"),
    "google_disconnected": (
        Operacao("onboarding", "google_desconectar", "write", "postgres", _PERSISTIDO),
        "succeeded"),
    "google_test_event_created": (
        Operacao("agenda", "evento_de_teste", "write", "google_calendar", _EXTERNO), "succeeded"),
    "google_test_event_failed": (
        Operacao("agenda", "evento_de_teste", "write", "google_calendar"), "failed"),
    # Canal: a dependência (whatsapp/telegram) vem do payload.
    "message_received": (Operacao("canal", "recepcao", "delivery"), "succeeded"),
    "message_sent": (Operacao("canal", "envio", "delivery"), "succeeded"),
    "message_duplicated": (Operacao("canal", "recepcao", "delivery"), "attempted"),
    "proactive_sent": (Operacao("canal", "proatividade", "delivery"), "succeeded"),
    "proactive_channel": (Operacao("canal", "proatividade", "delivery"), "attempted"),
    "proactive_failed": (Operacao("canal", "proatividade", "delivery"), "failed"),
    "dashboard_sent": (Operacao("canal", "dashboard", "delivery"), "succeeded"),
    # Sistema: o que o Mordomo faz por dentro para atender qualquer capacidade.
    "orchestrator_decision": (Operacao("sistema", "rotear", "system", "llm"), "succeeded"),
    "orchestrator_parse_error": (Operacao("sistema", "rotear", "system", "llm"), "failed"),
    "llm_usage": (Operacao("sistema", "llm", "system", "llm"), "succeeded"),
    "turn_completed": (Operacao("sistema", "turno", "system"), "succeeded"),
    "error": (Operacao("sistema", "erro", "system"), "failed"),
    "unknown_user": (Operacao("sistema", "desconhecido", "system"), "failed"),
    "curation_run": (Operacao("sistema", "curadoria", "system"), "succeeded"),
    "feature_requested": (Operacao("pedidos", "registrar", "write", "postgres"), "attempted"),
    "feature_issue_created": (Operacao("pedidos", "issue", "write"), "succeeded"),
}

_OPERACOES_COM_TOOL = {(op.capability, op.operation) for op in TOOLS.values()}

# Jornada: fato de CICLO DE VIDA, não operação sobre o domínio (por isso
# kind="system"). Fica fora das contagens de operação — quem soma jornada é
# `queries.jornadas` — mas ganha capacidade para o painel cruzar as duas visões.
CAPACIDADE_POR_JORNADA = {"task": "tarefas", "calendar_create": "agenda"}

_EVIDENCIA_DA_JORNADA = {
    "journey_started": "attempted",
    "journey_resolved": "resolved",
    "journey_abandoned": "abandoned",
    "journey_reopened": "attempted",
}

# Canal informado no payload → dependência. Fora desta lista, None.
_DEPENDENCIA_POR_CANAL = {"whatsapp": "whatsapp", "telegram": "telegram"}
# Destino informado pela Agenda → dependência comprovada.
_DEPENDENCIA_POR_DESTINO = {"google": "google_calendar", "nativo": "postgres"}


def tipos_de_operacao() -> tuple[str, ...]:
    """Tipos de evento que SOZINHOS já descrevem uma operação de capacidade.

    São os fatos de domínio que não passam por tool nenhuma (o documento
    guardado pelo adapter, o OAuth do Google, o convite consumido). Os que
    acompanham um `tool_result` ficam de fora — quem os conta é o funil da
    tool, e contar duas vezes inflaria a capacidade."""
    return tuple(
        tipo
        for tipo, (op, _) in _EVENTOS.items()
        if op.kind in ("read", "write") and not coberta_por_tool(op.capability, op.operation)
    )


def coberta_por_tool(capability: str, operation: str) -> bool:
    """A operação já é contada pelo funil de tools?

    Existe porque alguns fatos de domínio acompanham o `tool_result` que os
    gerou (`reminder_created` sai junto de `criar_lembrete`): contar os dois
    dobraria a capacidade. Já `document_stored` não tem tool nenhuma — a guarda
    acontece no adapter, fora do turno — e por isso precisa entrar."""
    return (capability, operation) in _OPERACOES_COM_TOOL


def capacidade_da_jornada(journey_type: str | None) -> str:
    """A capacidade dona de uma jornada; tipo novo cai em `outros` sem quebrar."""
    return CAPACIDADE_POR_JORNADA.get(journey_type or "", "outros")


def _dependencia(op: Operacao, payload: dict) -> str | None:
    """O payload manda quando PROVA a dependência; senão vale o padrão da
    operação. `message_status` de um canal desconhecido não vira "whatsapp"."""
    if (destino := payload.get("destino")) and op.capability == "agenda":
        return _DEPENDENCIA_POR_DESTINO.get(destino)
    if canal := payload.get("canal"):
        return _DEPENDENCIA_POR_CANAL.get(canal, op.dependency)
    return op.dependency


_EVIDENCIA_DO_STATUS = {
    "sent": "attempted",
    "delivered": "succeeded",
    "read": "succeeded",
    "failed": "failed",
}


def projetar(tipo: str, payload: dict | None) -> Projecao | None:
    """Evento → dimensões estáveis. None quando o tipo não é classificável.

    None é resposta legítima e VISÍVEL: o painel mostra quanto do volume ficou
    sem capacidade, que é o sinal de "instrumentaram algo novo e esqueceram a
    taxonomia"."""
    payload = payload or {}

    if tipo in ("tool_called", "tool_result"):
        op = TOOLS.get(payload.get("tool") or "")
        if op is None:
            return None
        if tipo == "tool_called":
            evidencia = "attempted"
        else:
            evidencia = "succeeded" if payload.get("ok") else "failed"
        return _montar(op, payload, evidencia)

    if tipo in _EVIDENCIA_DA_JORNADA:
        capacidade = capacidade_da_jornada(payload.get("journey_type"))
        return Projecao(
            capability=capacidade,
            operation="jornada",
            kind="system",
            dependency=None,
            evidence=_EVIDENCIA_DA_JORNADA[tipo],
            prova=None,
        )

    if tipo == "message_status":
        op = Operacao("canal", "entrega", "delivery")
        evidencia = _EVIDENCIA_DO_STATUS.get(payload.get("status") or "", "attempted")
        return _montar(op, payload, evidencia)

    if (achado := _EVENTOS.get(tipo)) is not None:
        op, evidencia = achado
        if tipo == "turn_completed" and payload.get("ok") is False:
            evidencia = "failed"
        if tipo == "feature_issue_created" and not payload.get("ok"):
            evidencia = "failed"
        return _montar(op, payload, evidencia)

    return None


def _montar(op: Operacao, payload: dict, evidencia: str) -> Projecao:
    return Projecao(
        capability=op.capability,
        operation=op.operation,
        kind=op.kind,
        dependency=_dependencia(op, payload),
        evidence=evidencia,
        prova=op.prova,
    )
