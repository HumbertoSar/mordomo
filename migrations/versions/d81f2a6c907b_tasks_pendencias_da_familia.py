"""tasks: pendências acompanháveis da família

Revision ID: d81f2a6c907b
Revises: c4a91d7e2b10
Create Date: 2026-08-15

ESCRITA À MÃO (não autogenerate — ver o gotcha do checkpointer no CLAUDE.md).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d81f2a6c907b"
down_revision: str | Sequence[str] | None = "c4a91d7e2b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria o estado operacional das tarefas; Analytics continua append-only."""
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("titulo", sa.String(length=300), nullable=False),
        sa.Column("criado_por", sa.Integer(), nullable=False),
        sa.Column("responsavel_id", sa.Integer(), nullable=True),
        sa.Column("compartilhada", sa.Boolean(), nullable=False),
        sa.Column("prazo_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("journey_id", sa.String(length=64), nullable=False),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["criado_por"], ["members.id"]),
        sa.ForeignKeyConstraint(["responsavel_id"], ["members.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_criado_por", "tasks", ["criado_por"], unique=False)
    op.create_index("ix_tasks_responsavel_id", "tasks", ["responsavel_id"], unique=False)
    op.create_index("ix_tasks_status", "tasks", ["status"], unique=False)
    op.create_index("ix_tasks_journey_id", "tasks", ["journey_id"], unique=True)


def downgrade() -> None:
    """Remove a feature; product_events históricos permanecem preservados."""
    op.drop_index("ix_tasks_journey_id", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_responsavel_id", table_name="tasks")
    op.drop_index("ix_tasks_criado_por", table_name="tasks")
    op.drop_table("tasks")
