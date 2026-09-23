from sqlalchemy import Computed, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

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
