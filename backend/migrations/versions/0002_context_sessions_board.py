"""context: sessions and board_entries

Revision ID: 0002_context
Revises: 0001_kit_records
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_context"
down_revision = "0001_kit_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "board_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("source_id", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("ts_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("ts_end_ms", sa.BigInteger(), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_board_entries_session_id", "board_entries", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_board_entries_session_id", table_name="board_entries")
    op.drop_table("board_entries")
    op.drop_table("sessions")
