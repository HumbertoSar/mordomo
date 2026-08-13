"""/conectar: anexar canal a um membro existente

Revision ID: b93e77c1a4f8
Revises: a17c3e90b4d2
Create Date: 2026-08-13

ESCRITA À MÃO (não autogenerate — ver o gotcha do checkpointer no CLAUDE.md).

Uma coluna só: `invite_codes.conectar_member_id`. Quando preenchida, o código
não cria membro novo — anexa a identidade do canal ao membro apontado. Sem
isso, quem já usa o Telegram e se "vincula" no WhatsApp vira DUAS pessoas para
o mordomo e perde lembretes, cofre e histórico na migração.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b93e77c1a4f8'
down_revision: Union[str, Sequence[str], None] = 'a17c3e90b4d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch_alter_table e não add_column + create_foreign_key: o SQLite não tem
    # ALTER TABLE ADD CONSTRAINT, e a cadeia de migrações é validada num SQLite
    # descartável antes de tocar a produção. No Postgres o batch vira o ALTER
    # normal; só o SQLite paga a estratégia de copiar-e-mover.
    with op.batch_alter_table('invite_codes') as batch:
        batch.add_column(sa.Column('conectar_member_id', sa.Integer(), nullable=True))
        # FK nomeada: constraint anônima não tem como ser dropada no downgrade.
        batch.create_foreign_key(
            'fk_invite_codes_conectar_member_id', 'members',
            ['conectar_member_id'], ['id'],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('invite_codes') as batch:
        batch.drop_constraint('fk_invite_codes_conectar_member_id', type_='foreignkey')
        batch.drop_column('conectar_member_id')
