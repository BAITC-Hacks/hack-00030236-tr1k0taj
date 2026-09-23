"""tracer: trace_spans (persistent OpenTelemetry spans, ADR 0013)

Revision ID: 0004_trace_spans
Revises: 0003_knowledge_vectors
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_trace_spans"
down_revision = "0003_knowledge_vectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trace_spans",
        sa.Column("span_id", sa.Text(), primary_key=True),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("parent_span_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("start_ns", sa.BigInteger(), nullable=True),
        sa.Column("end_ns", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        sa.Column("events", postgresql.JSONB(), nullable=False),
        sa.Column("links", postgresql.JSONB(), nullable=False),
        sa.Column("resource", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_trace_spans_trace_id", "trace_spans", ["trace_id"])
    op.create_index("ix_trace_spans_session_start", "trace_spans", ["session_id", "start_ns"])


def downgrade() -> None:
    op.drop_index("ix_trace_spans_session_start", table_name="trace_spans")
    op.drop_index("ix_trace_spans_trace_id", table_name="trace_spans")
    op.drop_table("trace_spans")
