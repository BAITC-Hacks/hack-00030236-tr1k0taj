"""kit_records: starter kit loaded into Postgres (JSONB + FTS + trigram)

Revision ID: 0001_kit_records
Revises:
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_kit_records"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "kit_records",
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', search_text)", persisted=True),
        ),
        sa.Column("origin", sa.String(8), nullable=False, server_default="kit"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_kit_records_tsv", "kit_records", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_kit_records_trgm",
        "kit_records",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_table("kit_records")
