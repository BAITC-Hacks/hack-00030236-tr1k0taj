"""Public document embeddings with transactional invalidation.

Revision ID: 0003_knowledge_vectors
Revises: 0002_context
"""

from alembic import op

revision = "0003_knowledge_vectors"
down_revision = "0002_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Unbounded vector supports configurable embedding dimensions; the tiny kit uses exact search.
    op.execute("""
        CREATE TABLE knowledge_vectors (
            kind varchar(32) NOT NULL,
            key varchar(128) NOT NULL,
            model varchar(128) NOT NULL,
            dimensions integer NOT NULL CHECK (dimensions > 0),
            text_hash varchar(32) NOT NULL,
            embedding vector NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (kind, key),
            FOREIGN KEY (kind, key) REFERENCES kit_records(kind, key) ON DELETE CASCADE,
            CHECK (kind IN ('kb', 'office', 'clinic', 'inspection_point')),
            CHECK (vector_dims(embedding) = dimensions)
        )
    """)
    op.execute("""
        CREATE FUNCTION invalidate_knowledge_vector() RETURNS trigger AS $$
        BEGIN
            DELETE FROM knowledge_vectors WHERE kind = OLD.kind AND key = OLD.key;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER kit_record_vector_invalidation AFTER UPDATE OF search_text ON kit_records
        FOR EACH ROW WHEN (OLD.search_text IS DISTINCT FROM NEW.search_text)
        EXECUTE FUNCTION invalidate_knowledge_vector()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS kit_record_vector_invalidation ON kit_records")
    op.execute("DROP FUNCTION IF EXISTS invalidate_knowledge_vector()")
    op.drop_table("knowledge_vectors")
