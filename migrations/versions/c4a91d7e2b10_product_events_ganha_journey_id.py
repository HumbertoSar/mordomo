"""product_events ganha journey_id

Revision ID: c4a91d7e2b10
Revises: b93e77c1a4f8
Create Date: 2026-08-15

ESCRITA À MÃO (não autogenerate — ver o gotcha do checkpointer no CLAUDE.md).

A coluna é nullable para preservar todo o histórico: turnos antigos continuam
válidos, mas não fingem pertencer a uma jornada que nunca foi observada.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4a91d7e2b10"
down_revision: Union[str, Sequence[str], None] = "b93e77c1a4f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Adiciona a correlação de jornada sem reescrever eventos antigos."""
    with op.batch_alter_table("product_events") as batch:
        batch.add_column(sa.Column("journey_id", sa.String(length=64), nullable=True))
        batch.create_index("ix_product_events_journey_id", ["journey_id"], unique=False)


def downgrade() -> None:
    """Remove somente a correlação; os demais eventos permanecem intactos."""
    with op.batch_alter_table("product_events") as batch:
        batch.drop_index("ix_product_events_journey_id")
        batch.drop_column("journey_id")
