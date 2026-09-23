"""Модуль tracer: трасса хода в формате OpenTelemetry (ADR 0013, docs/specs/tracer-module.md).

Нижний слой: не импортирует другие модули приложения. Kernel/call оборачивают этапы в span(),
TraceMiddleware продолжает W3C traceparent браузера, api_router отдаёт трассы панели.
"""

from app.tracer.api import SpanView, TraceSummary, TraceView
from app.tracer.api import router as api_router
from app.tracer.middleware import TraceMiddleware
from app.tracer.setup import (
    current_span_context,
    current_trace_id,
    get_tracer,
    setup_tracing,
    span,
)

__all__ = [
    "SpanView",
    "TraceMiddleware",
    "TraceSummary",
    "TraceView",
    "api_router",
    "current_span_context",
    "current_trace_id",
    "get_tracer",
    "setup_tracing",
    "span",
]
