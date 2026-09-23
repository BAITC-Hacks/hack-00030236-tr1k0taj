from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.db import Base


class KitRecord(Base):
    """One starter-kit object (scenario, client, KB chunk, ...) keyed by (kind, key)."""

    __tablename__ = "kit_records"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[object] = mapped_column(JSONB)
    search_text: Mapped[str] = mapped_column(Text, default="")
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('simple', search_text)", persisted=True))
    origin: Mapped[str] = mapped_column(String(8), default="kit")  # kit | user
    updated_at = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_kit_records_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_kit_records_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )


class VectorType(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "vector"


class KnowledgeVector(Base):
    """Vector width is per row to support model changes without changing the table type."""

    __tablename__ = "knowledge_vectors"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Several vectors per record: 0 = original text, 1.. = ru/kk/mixed questions (ADR 0010).
    # text_hash versions the whole record (md5 of kit_records.search_text), not the variant.
    idx: Mapped[int] = mapped_column(primary_key=True, server_default="0")
    model: Mapped[str] = mapped_column(String(128))
    dimensions: Mapped[int]
    text_hash: Mapped[str] = mapped_column(String(32))
    embedding = mapped_column(VectorType(), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["kind", "key"], ["kit_records.kind", "kit_records.key"], ondelete="CASCADE"
        ),
        CheckConstraint("kind IN ('kb', 'office', 'clinic', 'inspection_point')"),
        CheckConstraint("dimensions > 0"),
        CheckConstraint("vector_dims(embedding) = dimensions"),
    )
