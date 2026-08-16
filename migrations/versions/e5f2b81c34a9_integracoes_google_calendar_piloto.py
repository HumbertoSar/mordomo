"""integrações: piloto Google Calendar (oauth_states + google_connections)

Revision ID: e5f2b81c34a9
Revises: d81f2a6c907b
Create Date: 2026-08-16

ESCRITA À MÃO, não autogenerate — o gotcha do checkpointer no CLAUDE.md: as
tabelas do LangGraph (checkpoint*) vivem neste mesmo banco e NÃO estão em
Base.metadata, então o autogenerate escreveria drop_table para elas e apagaria
o histórico de conversas da família. Esta migração só CRIA tabelas novas e não
encosta em nada que já existe.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f2b81c34a9"
down_revision: str | Sequence[str] | None = "d81f2a6c907b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # state do OAuth: prova de que o callback responde a um pedido nosso e
    # amarra o code que volta do Google a um member_id (nunca a query string).
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provedor", sa.String(length=20), nullable=False),
        # só o HASH do state: dump do banco não permite forjar callback
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usado_em", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE, não só índice: é o que garante o uso único mesmo com dois
    # callbacks simultâneos (retry do navegador).
    op.create_index("ix_oauth_states_state_hash", "oauth_states", ["state_hash"], unique=True)
    op.create_index("ix_oauth_states_member_id", "oauth_states", ["member_id"], unique=False)

    # Credencial do Google de um membro. Tokens CIFRADOS (Fernet); a chave
    # mora só em GOOGLE_TOKEN_KEY, nunca aqui. Sem e-mail, sem nome da conta.
    op.create_table(
        "google_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("access_token_cifrado", sa.Text(), nullable=False),
        # nulo é normal: o Google só devolve refresh no 1º consentimento
        sa.Column("refresh_token_cifrado", sa.Text(), nullable=True),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escopo", sa.String(length=300), nullable=False),
        sa.Column("teste_event_id", sa.String(length=120), nullable=True),
        sa.Column("teste_link", sa.String(length=500), nullable=True),
        sa.Column("teste_criado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # Um membro, UMA conexão: é o UNIQUE que faz o segundo callback simultâneo
    # cair no caminho de UPDATE em vez de criar uma conexão órfã.
    op.create_index(
        "ix_google_connections_member_id", "google_connections", ["member_id"], unique=True
    )


def downgrade() -> None:
    """Remove a integração. product_events históricos permanecem preservados —
    e desfazer aqui APAGA os tokens, que é o comportamento desejado: ninguém
    quer credencial órfã de uma feature revertida."""
    op.drop_index("ix_google_connections_member_id", table_name="google_connections")
    op.drop_table("google_connections")
    op.drop_index("ix_oauth_states_member_id", table_name="oauth_states")
    op.drop_index("ix_oauth_states_state_hash", table_name="oauth_states")
    op.drop_table("oauth_states")
