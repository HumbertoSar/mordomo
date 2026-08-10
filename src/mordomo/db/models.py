"""Modelos: domínio (membros, lembretes, eventos da família) + analytics
(eventos de produto — camada 1-3 do doc `gestao-a-vista-agente-whatsapp.md`)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def agora_utc() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# ── Domínio ──────────────────────────────────────────────────────────────


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))
    papel: Mapped[str] = mapped_column(String(20), default="adulto")  # adulto | crianca
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora_utc)

    identidades: Mapped[list[ChannelIdentity]] = relationship(back_populates="member")


class ChannelIdentity(Base):
    """Identidade por canal (ADR-003): telegram user_id, wa_id (telefone)…
    O resto do sistema só enxerga member_id."""

    __tablename__ = "channel_identities"
    __table_args__ = (UniqueConstraint("canal", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    canal: Mapped[str] = mapped_column(String(20))          # "telegram" | "whatsapp"
    external_id: Mapped[str] = mapped_column(String(64))    # tg user_id / wa_id

    member: Mapped[Member] = relationship(back_populates="identidades")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    texto: Mapped[str] = mapped_column(String(500))
    quando_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pendente")  # pendente|enviado|cancelado
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora_utc)


class FamilyEvent(Base):
    """Agenda simples no banco para o MVP rodar no dia 1.
    Fase 2: trocar por Google Calendar (tool nativa ou MCP) — ver ADR em docs/adr."""

    __tablename__ = "family_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    titulo: Mapped[str] = mapped_column(String(200))
    inicio_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    local: Mapped[str | None] = mapped_column(String(200), nullable=True)
    criado_por: Mapped[int] = mapped_column(ForeignKey("members.id"))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora_utc)


# ── Analytics (eventos de produto) ───────────────────────────────────────


class ProductEvent(Base):
    """Evento estruturado: message_received, orchestrator_decision, tool_called,
    tool_result, llm_usage, turn_completed, message_sent, reminder_fired… O
    dashboard de gestão à vista lê de agregações sobre esta tabela (nunca do
    dado bruto direto).

    Três chaves de análise, do mais largo ao mais fino:
      member_id  — quem
      session_id — a conversa do dia ("<member_id>:<data>")
      turn_id    — UMA pergunta e sua resposta; é o que permite reconstruir o
                   funil (recebi → roteei → chamei tool → respondi) e atribuir
                   latência e custo a um turno específico.

    Só guardamos FATOS aqui (tokens, ms, ok/erro). Métrica derivada — custo em
    dólar, p95, taxa de sucesso — é responsabilidade de reporting/queries.py:
    assim o preço de um modelo pode mudar sem reescrever o histórico."""

    __tablename__ = "product_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora_utc, index=True)
    tipo: Mapped[str] = mapped_column(String(50), index=True)
    member_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
