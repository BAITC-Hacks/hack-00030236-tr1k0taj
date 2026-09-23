"""TracerProvider процесса, хелпер span() и экспорт в OTLP с маскированием ПД."""

import asyncio
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Link, SpanContext, SpanKind, Status, StatusCode

from app.tracer.redact import redact
from app.tracer.store import SpanStore

TRACER_NAME = "voice-router"

_provider: TracerProvider | None = None
_store: SpanStore | None = None


class _RedactingExporter(SpanExporter):
    """Обёртка над OTLP-экспортёром: маскирует ПД, `local.*` отправляет только по флагу."""

    def __init__(self, inner: SpanExporter, export_content: bool) -> None:
        self.inner = inner
        self.export_content = export_content

    def _clean(self, span: ReadableSpan) -> ReadableSpan:
        keep = self.export_content
        return ReadableSpan(
            name=span.name, context=span.context, parent=span.parent, resource=span.resource,
            attributes=redact(span.attributes, keep_local=keep),
            events=[type(e)(e.name, redact(e.attributes, keep_local=keep), e.timestamp)
                    for e in span.events],
            links=span.links, kind=span.kind, status=span.status,
            start_time=span.start_time, end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([self._clean(s) for s in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def _otlp_exporter(endpoint: str | None) -> SpanExporter | None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    if endpoint:
        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        return OTLPSpanExporter(endpoint=endpoint)
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    ):
        return OTLPSpanExporter()  # SDK сам читает OTEL_EXPORTER_OTLP_* из env
    return None


def setup_tracing(
    service_name: str = "voice-router-backend",
    *,
    otlp_endpoint: str | None = None,
    buffer_spans: int = 5000,
) -> TracerProvider:
    """Идемпотентно ставит глобальный TracerProvider: локальное хранилище + OTLP, если задан."""
    global _provider, _store
    if _provider is not None:
        return _provider
    name = os.environ.get("OTEL_SERVICE_NAME") or service_name
    provider = TracerProvider(resource=Resource.create({"service.name": name}))
    store = SpanStore(max_spans=buffer_spans)
    provider.add_span_processor(store)
    exporter = _otlp_exporter(otlp_endpoint)
    if exporter is not None:
        export_content = os.environ.get("TRACE_EXPORT_CONTENT", "").lower() in {"1", "true", "yes"}
        provider.add_span_processor(BatchSpanProcessor(_RedactingExporter(exporter, export_content)))
    trace.set_tracer_provider(provider)
    _provider, _store = provider, store
    return provider


def get_store() -> SpanStore | None:
    return _store


def get_tracer(name: str = TRACER_NAME) -> trace.Tracer:
    return trace.get_tracer(name)


@contextmanager
def span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    /,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    links: Sequence[SpanContext] = (),
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Дочерний span текущего контекста. Атрибуты с точками — через словарь:
    `span("turn", {"session.id": sid}, links=[ctx])`; простые — kwargs.

    Исключение → record_exception + статус ERROR. Отмена (CancelledError, закрытие генератора)
    → событие `cancelled`, исключение пробрасывается дальше.
    """
    merged = {**(attributes or {}), **attrs}
    with get_tracer().start_as_current_span(
        name, kind=kind, attributes={k: v for k, v in merged.items() if v is not None},
        links=[Link(c) for c in links if c.is_valid],
        record_exception=False, set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except (asyncio.CancelledError, GeneratorExit):
            current.add_event("cancelled")
            raise
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise


def current_span_context() -> SpanContext | None:
    """Контекст текущего span'а — для link из фоновых `agent.run`."""
    ctx = trace.get_current_span().get_span_context()
    return ctx if ctx.is_valid else None


def current_trace_id() -> str | None:
    ctx = current_span_context()
    return f"{ctx.trace_id:032x}" if ctx else None
