"""Модуль tracer: сквозная трасса звонка в формате OpenTelemetry (ADR 0013, docs/specs/tracer-module.md).

Нижний слой: не импортирует другие модули приложения — фабрику сессий БД и engine передаёт
вызывающий. Kernel/call оборачивают этапы в span(), TraceMiddleware продолжает W3C traceparent
браузера, start_persistence пишет span'ы в `trace_spans`, api_router отдаёт трассы и историю.
"""

from app.tracer.api import router as api_router
from app.tracer.middleware import TraceMiddleware
from app.tracer.models import trace_metadata, trace_spans
from app.tracer.persist import (
    flush_traces,
    persistence_stats,
    start_persistence,
    stop_persistence,
)
from app.tracer.setup import (
    current_span_context,
    current_trace_id,
    get_tracer,
    setup_tracing,
    span,
)
from app.tracer.sql import instrument_sqlalchemy, suppress_sql_spans
from app.tracer.views import (
    ModelUsage,
    SessionTrace,
    SessionTraceSummary,
    SpanView,
    TokenTotals,
    TraceSummary,
    TraceView,
    TurnTrace,
)

__all__ = [
    "ModelUsage",
    "SessionTrace",
    "SessionTraceSummary",
    "SpanView",
    "TokenTotals",
    "TraceMiddleware",
    "TraceSummary",
    "TraceView",
    "TurnTrace",
    "api_router",
    "current_span_context",
    "current_trace_id",
    "flush_traces",
    "get_tracer",
    "instrument_sqlalchemy",
    "persistence_stats",
    "setup_tracing",
    "span",
    "start_persistence",
    "stop_persistence",
    "suppress_sql_spans",
    "trace_metadata",
    "trace_spans",
]
