from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.db import Base


class SessionRow(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    id = synonym("session_id")  # compatibility for former kernel repository cleanup clients
    generation: Mapped[int] = mapped_column(Integer)
    context_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BoardEntryRow(Base):
    __tablename__ = "board_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    generation: Mapped[int] = mapped_column(Integer)
    turn_id: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String)
    author: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String)
    source_id: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float | None] = mapped_column(Float)
    ts_start_ms: Mapped[int] = mapped_column(BigInteger)
    ts_end_ms: Mapped[int] = mapped_column(BigInteger)
    context_version: Mapped[int] = mapped_column(Integer)
