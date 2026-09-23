"""knowledge_vectors: several vectors per record (original text + ru/kk questions)

Revision ID: 0005_knowledge_variants
Revises: 0004_trace_spans
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_knowledge_variants"
down_revision = "0004_trace_spans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_vectors",
        sa.Column("idx", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint("knowledge_vectors_pkey", "knowledge_vectors", type_="primary")
    op.create_primary_key("knowledge_vectors_pkey", "knowledge_vectors", ["kind", "key", "idx"])


def downgrade() -> None:
    op.execute("DELETE FROM knowledge_vectors WHERE idx > 0")
    op.drop_constraint("knowledge_vectors_pkey", "knowledge_vectors", type_="primary")
    op.create_primary_key("knowledge_vectors_pkey", "knowledge_vectors", ["kind", "key"])
    op.drop_column("knowledge_vectors", "idx")
