"""family_events ganha fim_utc (o término dito na conversa)

Revision ID: f3a71c9d2b45
Revises: e5f2b81c34a9
Create Date: 2026-08-16

ESCRITA À MÃO, não autogenerate — o gotcha do checkpointer no CLAUDE.md: as
tabelas do LangGraph (checkpoint*) vivem neste mesmo banco e NÃO estão em
Base.metadata, então o autogenerate escreveria drop_table para elas e apagaria
o histórico de conversas da família.

Coluna NULA por definição: os eventos já gravados não têm término nenhum a
recuperar, e inventar um (início + 1h) seria escrever no banco um dado que
ninguém disse.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a71c9d2b45"
down_revision: str | Sequence[str] | None = "e5f2b81c34a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "family_events",
        sa.Column("fim_utc", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("family_events", "fim_utc")
