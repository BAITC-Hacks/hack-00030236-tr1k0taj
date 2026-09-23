"""Persistent session projections and append-only agent event journal."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_agent_kernel"
down_revision = "0001_kit_records"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("state", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("envelope", JSONB(), nullable=False),
        sa.UniqueConstraint("session_id", "seq"),
    )
    op.create_index("ix_agent_events_replay", "agent_events", ["session_id", "visibility", "seq"])


def downgrade():
    op.drop_table("agent_events")
    op.drop_table("agent_sessions")
