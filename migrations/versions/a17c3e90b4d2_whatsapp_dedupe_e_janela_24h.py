"""whatsapp: dedupe por wamid + janela de 24h

Revision ID: a17c3e90b4d2
Revises: bc5623f8f339
Create Date: 2026-08-12

ESCRITA À MÃO de propósito (não é autogenerate): as tabelas do checkpointer do
LangGraph vivem neste mesmo banco e o autogenerate já tentou dropá-las uma vez
— ver o gotcha do CLAUDE.md sobre `include_object` em migrations/env.py.

Duas mudanças, ambas para a fase 3:
  - channel_messages: a Meta reenvia webhook por até 7 dias; o UNIQUE
    (canal, message_id) é o que impede o mesmo pedido virar dois lembretes.
  - channel_identities.ultima_entrada_em: marca a última mensagem recebida por
    identidade — é o relógio da janela de 24h do WhatsApp (dentro dela o
    proativo é free-form; fora, só template aprovado).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a17c3e90b4d2'
down_revision: Union[str, Sequence[str], None] = 'bc5623f8f339'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'channel_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('canal', sa.String(length=20), nullable=False),
        sa.Column('message_id', sa.String(length=128), nullable=False),
        sa.Column('criado_em', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('canal', 'message_id'),
    )
    op.create_index(
        op.f('ix_channel_messages_criado_em'), 'channel_messages', ['criado_em'], unique=False
    )
    # Nullable de propósito: identidade antiga (Telegram) nunca falou por
    # WhatsApp, e NULL é justamente "janela fechada" na leitura do adapter.
    op.add_column(
        'channel_identities',
        sa.Column('ultima_entrada_em', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('channel_identities', 'ultima_entrada_em')
    op.drop_index(op.f('ix_channel_messages_criado_em'), table_name='channel_messages')
    op.drop_table('channel_messages')
