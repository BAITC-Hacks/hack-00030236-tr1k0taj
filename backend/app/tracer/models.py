"""Таблица `trace_spans` (SQLAlchemy Core, своя MetaData: tracer не импортирует app.db).

Миграция — `migrations/versions/0004_trace_spans.py`; env.py добавляет `trace_metadata`
в `target_metadata`, чтобы autogenerate видел таблицу.
"""

from sqlalchemy import BigInteger, Column, Float, Index, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB

trace_metadata = MetaData()

trace_spans = Table(
    "trace_spans",
    trace_metadata,
    Column("span_id", Text, primary_key=True),
    Column("trace_id", Text, nullable=False),
    Column("parent_span_id", Text, nullable=True),
    Column("session_id", Text, nullable=True),
    Column("turn_id", Integer, nullable=True),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("start_ns", BigInteger, nullable=True),
    Column("end_ns", BigInteger, nullable=True),
    Column("duration_ms", Float, nullable=True),
    Column("status", Text, nullable=False),
    Column("status_message", Text, nullable=True),
    Column("attributes", JSONB, nullable=False),
    Column("events", JSONB, nullable=False),
    Column("links", JSONB, nullable=False),
    Column("resource", JSONB, nullable=False),
    Index("ix_trace_spans_trace_id", "trace_id"),
    Index("ix_trace_spans_session_start", "session_id", "start_ns"),
)

COLUMNS = tuple(c.name for c in trace_spans.columns)
