"""agenda: event_proposals (preparar → confirmar antes de criar)

Revision ID: a4c8e2f61b73
Revises: f3a71c9d2b45
Create Date: 2026-08-17

ESCRITA À MÃO, não autogenerate — o gotcha do checkpointer no CLAUDE.md: as
tabelas do LangGraph (checkpoint*) vivem neste mesmo banco e NÃO estão em
Base.metadata, então o autogenerate escreveria drop_table para elas e apagaria
o histórico de conversas da família. Esta migração só CRIA uma tabela nova e
não encosta em nada que já existe.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c8e2f61b73"
down_revision: str | Sequence[str] | None = "f3a71c9d2b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Compromisso RESOLVIDO à espera do "sim". Guarda o RESULTADO (instantes em
    # UTC, convidados, Meet, destino) justamente para a confirmação não precisar
    # reler a frase — foi reparsear depois do "sim" que quebrou uma criação real
    # em 17/08/2026 ("segunda, 24/08, das 9h").
    op.create_table(
        "event_proposals",
        sa.Column("id", sa.Integer(), nullable=False),
        # identidade opaca da proposta (é o que a confirmação reivindica)
        sa.Column("codigo", sa.String(length=32), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        # de qual turno/jornada nasceu — o "sim" vem em OUTRO turno
        sa.Column("turn_id", sa.String(length=32), nullable=True),
        sa.Column("journey_id", sa.String(length=64), nullable=True),
        sa.Column("titulo", sa.String(length=200), nullable=False),
        sa.Column("inicio_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fim_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("local", sa.String(length=200), nullable=True),
        sa.Column("convidados", sa.JSON(), nullable=False),
        sa.Column("com_meet", sa.Boolean(), nullable=False),
        sa.Column("destino", sa.String(length=10), nullable=False),  # google | nativo
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=False),
        # trava de uso único, tomada por UPDATE condicional
        sa.Column("usado_em", sa.DateTime(timezone=True), nullable=True),
        # distinto de usado_em: a API externa pode ainda estar em andamento
        sa.Column("concluido_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("link", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE, não só índice: o código é a identidade da proposta, e é por ele
    # que uma confirmação com código explícito chega a UMA linha só.
    op.create_index("ix_event_proposals_codigo", "event_proposals", ["codigo"], unique=True)
    # Por membro: é o acesso de todo "sim" sem código (ADR-003 — o pendente é
    # sempre procurado a partir de quem está falando).
    op.create_index(
        "ix_event_proposals_member_id", "event_proposals", ["member_id"], unique=False
    )


def downgrade() -> None:
    """Remove as propostas pendentes. Nada de agenda se perde: proposta NÃO é
    compromisso — o que já foi confirmado virou evento no Google ou linha em
    family_events, e ambos continuam onde estão."""
    op.drop_index("ix_event_proposals_member_id", table_name="event_proposals")
    op.drop_index("ix_event_proposals_codigo", table_name="event_proposals")
    op.drop_table("event_proposals")
