"""Modelos: domínio (membros, lembretes, eventos da família) + analytics
(eventos de produto — camada 1-3 do doc `gestao-a-vista-agente-whatsapp.md`)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def agora_utc() -> datetime:
    return datetime.now(UTC)


class TZDateTime(TypeDecorator):
    """DateTime que SEMPRE volta timezone-aware.

    O Postgres respeita timezone=True; o SQLite (dev/testes) devolve naive —
    e naive não compara nem converte com aware: `carregar_pendentes` explodia
    no boot em dev, e `astimezone` interpretava o valor como hora local da
    máquina (3h de erro na exibição). Todo valor é gravado em UTC; na leitura,
    naive significa UTC e ganha o tzinfo de volta."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


# ── Domínio ──────────────────────────────────────────────────────────────


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))
    papel: Mapped[str] = mapped_column(String(20), default="adulto")  # adulto | crianca
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc)

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
    # Última mensagem RECEBIDA desta identidade. No WhatsApp é o que abre a
    # janela de 24h: dentro dela o proativo é free-form; fora, só template
    # aprovado (regra da Meta, não nossa). None = nunca falou por aqui.
    ultima_entrada_em: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    member: Mapped[Member] = relationship(back_populates="identidades")


class ChannelMessage(Base):
    """Mensagem de entrada já processada — dedupe de webhook.

    A Meta reenvia o webhook até receber 200 e insiste por ATÉ 7 DIAS. Sem esta
    trava, um deploy demorado (ou um erro nosso de 30s) vira a mesma pergunta
    processada N vezes: N lembretes criados, N respostas, N custos de LLM.
    Dedupe em memória não serve — o retry sobrevive ao restart, o dicionário não.

    Vale para qualquer canal com entrega at-least-once; o Telegram (long
    polling, confirmação por offset) não precisa e não usa."""

    __tablename__ = "channel_messages"
    __table_args__ = (UniqueConstraint("canal", "message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    canal: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str] = mapped_column(String(128))    # wamid do WhatsApp
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc, index=True)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    texto: Mapped[str] = mapped_column(String(500))
    quando_utc: Mapped[datetime] = mapped_column(TZDateTime)
    status: Mapped[str] = mapped_column(String(20), default="pendente")  # pendente|enviado|cancelado
    # Regra serializada ("diaria:@08:00", "semanal:0@07:30", "mensal:5@09:00").
    # None = lembrete único. Recorrente dispara e REAGENDA (quando_utc avança);
    # só sai de "pendente" quando cancelado.
    recorrencia: Mapped[str | None] = mapped_column(String(40), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc)


class FamilyEvent(Base):
    """Agenda simples no banco para o MVP rodar no dia 1.
    Fase 2: trocar por Google Calendar (tool nativa ou MCP) — ver ADR em docs/adr."""

    __tablename__ = "family_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    titulo: Mapped[str] = mapped_column(String(200))
    inicio_utc: Mapped[datetime] = mapped_column(TZDateTime)
    local: Mapped[str | None] = mapped_column(String(200), nullable=True)
    criado_por: Mapped[int] = mapped_column(ForeignKey("members.id"))
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc)


class Task(Base):
    """Pendência acompanhável: o primeiro domínio que fecha uma jornada real."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    titulo: Mapped[str] = mapped_column(String(300))
    criado_por: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    responsavel_id: Mapped[int | None] = mapped_column(
        ForeignKey("members.id"), nullable=True, index=True
    )
    compartilhada: Mapped[bool] = mapped_column(default=False)
    prazo_utc: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="aberta", index=True)
    journey_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc)
    atualizado_em: Mapped[datetime] = mapped_column(
        TZDateTime, default=agora_utc, onupdate=agora_utc
    )


class VaultItem(Base):
    """Cofre da família: dado estruturado que se consulta sempre (CEP, número
    de documento, carteirinha do plano…).

    `compartilhado=True` (padrão) = a família toda lê — cofre de família é
    para servir a família. False = só o dono. O VALOR nunca aparece em
    product_events nem nos traces (ADR-005): payloads levam chave e id."""

    __tablename__ = "vault_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    chave: Mapped[str] = mapped_column(String(120))       # "CEP de casa", "RG do Davi"
    valor: Mapped[str] = mapped_column(String(500))
    dono: Mapped[int] = mapped_column(ForeignKey("members.id"))
    compartilhado: Mapped[bool] = mapped_column(default=True)
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc)
    atualizado_em: Mapped[datetime] = mapped_column(
        TZDateTime, default=agora_utc, onupdate=agora_utc
    )


class Document(Base):
    """Documento/imagem da família (RG, carteirinha do plano, comprovante…).

    Os bytes moram AQUI (Postgres da VPS) e nunca passam pelo LLM nem pelos
    traces (ADR-005): o subagente só enxerga id + nome, e o adapter carrega os
    bytes na hora de enviar. `telegram_file_id` é cache de reenvio — o Telegram
    permite reenviar por id sem subir os bytes de novo; canais futuros ignoram."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))        # "RG do Davi" (chave natural)
    dono: Mapped[int] = mapped_column(ForeignKey("members.id"))
    compartilhado: Mapped[bool] = mapped_column(default=True)
    mime: Mapped[str] = mapped_column(String(100), default="image/jpeg")
    tamanho: Mapped[int] = mapped_column(default=0)       # bytes, para o dashboard
    dados: Mapped[bytes] = mapped_column(LargeBinary)
    telegram_file_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc)


class InviteCode(Base):
    """Convite de vínculo (/convidar → /vincular): onboarding SEM seed script.

    O código carrega nome e papel decididos por quem convidou — o convidado não
    escolhe o próprio papel (permissão nasce na borda, ADR-003). Uso único, com
    validade; o código é o segredo, então curto mas não adivinhável."""

    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    codigo: Mapped[str] = mapped_column(String(16), unique=True)
    nome: Mapped[str] = mapped_column(String(80))          # nome do futuro membro
    papel: Mapped[str] = mapped_column(String(20))         # adulto | crianca
    criado_por: Mapped[int] = mapped_column(ForeignKey("members.id"))
    # CONEXÃO de canal (/conectar), não convite: quando preenchido, consumir o
    # código NÃO cria membro — anexa a identidade do canal novo a ESTE membro.
    # É o que faz a migração Telegram → WhatsApp preservar histórico, cofre e
    # lembretes (ADR-003: thread = membro, e o membro é o mesmo).
    conectar_member_id: Mapped[int | None] = mapped_column(
        ForeignKey("members.id"), nullable=True
    )
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc)
    expira_em: Mapped[datetime] = mapped_column(TZDateTime)
    usado_por: Mapped[int | None] = mapped_column(ForeignKey("members.id"), nullable=True)
    usado_em: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


# ── Integrações OAuth (piloto Google Calendar — ADR-010) ─────────────────


class OAuthState(Base):
    """`state` do OAuth: prova de que o callback responde a um pedido NOSSO.

    Guarda o HASH do state, nunca o valor: quem tiver o dump do banco não
    consegue forjar um callback. É o que amarra o código que volta do Google a
    um `member_id` — a vinculação NUNCA vem da query string do usuário, que é
    exatamente o buraco por onde entraria "conectei o calendário do vizinho".

    Uso único (`usado_em`) e validade curta: sem isso, um callback repetido do
    histórico do navegador reabriria a troca de código."""

    __tablename__ = "oauth_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    provedor: Mapped[str] = mapped_column(String(20), default="google")
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc)
    expira_em: Mapped[datetime] = mapped_column(TZDateTime)
    usado_em: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class GoogleConnection(Base):
    """Credencial do Google de UM membro (um membro, uma conexão).

    Os tokens ficam CIFRADOS (integracoes/cripto.py) e a chave mora só em
    variável de ambiente: dump do Postgres não abre calendário de ninguém.
    Não guardamos e-mail nem nome da conta Google — o piloto não precisa
    saber QUEM é a conta, só escrever no `primary` dela (ADR-005)."""

    __tablename__ = "google_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), unique=True, index=True)
    access_token_cifrado: Mapped[str] = mapped_column(Text)
    # None é normal: o Google só devolve refresh_token no PRIMEIRO consentimento.
    refresh_token_cifrado: Mapped[str | None] = mapped_column(Text, nullable=True)
    expira_em: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    escopo: Mapped[str] = mapped_column(String(300), default="")
    # Idempotência do /google_teste: id determinístico do último evento, link
    # para repetir a resposta sem criar outro, e QUANDO foi — é a janela
    # deslizante que impede o comando repetido de virar dois eventos (ADR-010).
    teste_event_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    teste_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    teste_criado_em: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc)
    atualizado_em: Mapped[datetime] = mapped_column(
        TZDateTime, default=agora_utc, onupdate=agora_utc
    )


# ── Analytics (eventos de produto) ───────────────────────────────────────


class ProductEvent(Base):
    """Evento estruturado: message_received, orchestrator_decision, tool_called,
    tool_result, llm_usage, turn_completed, message_sent, reminder_fired… O
    dashboard de gestão à vista lê de agregações sobre esta tabela (nunca do
    dado bruto direto).

    Quatro chaves de análise, do mais largo ao mais fino:
      member_id  — quem
      session_id — a conversa do dia ("<member_id>:<data>")
      journey_id — uma necessidade que pode atravessar turnos e dias
      turn_id    — UMA pergunta e sua resposta; é o que permite reconstruir o
                   funil (recebi → roteei → chamei tool → respondi) e atribuir
                   latência e custo a um turno específico.

    Só guardamos FATOS aqui (tokens, ms, ok/erro). Métrica derivada — custo em
    dólar, p95, taxa de sucesso — é responsabilidade de reporting/queries.py:
    assim o preço de um modelo pode mudar sem reescrever o histórico."""

    __tablename__ = "product_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, default=agora_utc, index=True)
    tipo: Mapped[str] = mapped_column(String(50), index=True)
    member_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    journey_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
