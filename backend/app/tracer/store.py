"""Ограниченное in-process хранилище завершённых span'ов для панели трассировки.

SpanStore — это SpanProcessor: SDK вызывает on_end из любого потока, поэтому всё под lock'ом.
Храним не больше max_spans span'ов; при переполнении выбрасываем трассы, которые дольше всех
не получали новых span'ов. После рестарта процесса хранилище пустое (ADR 0013).
"""

import threading
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from app.tracer.redact import redact

SESSION_ATTR = "session.id"
TURN_ATTR = "turn.id"


def _iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).isoformat()


def _plain(attributes) -> dict[str, Any]:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in redact(attributes).items()}


def to_record(span: ReadableSpan) -> dict[str, Any]:
    """ReadableSpan → dict в форме, близкой к OTLP (идентификаторы в hex)."""
    ctx = span.context
    start, end = span.start_time, span.end_time
    return {
        "trace_id": f"{ctx.trace_id:032x}",
        "span_id": f"{ctx.span_id:016x}",
        "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
        "name": span.name,
        "kind": span.kind.name.lower(),
        "start_ns": start,
        "end_ns": end,
        "start_time": _iso(start),
        "end_time": _iso(end),
        "duration_ms": round((end - start) / 1e6, 3) if start and end else None,
        "status": span.status.status_code.name.lower(),
        "status_message": span.status.description,
        "attributes": _plain(span.attributes),
        "events": [
            {"name": e.name, "time": _iso(e.timestamp), "attributes": _plain(e.attributes)}
            for e in span.events
        ],
        "links": [
            {"trace_id": f"{link.context.trace_id:032x}", "span_id": f"{link.context.span_id:016x}"}
            for link in span.links
        ],
    }


class SpanStore(SpanProcessor):
    def __init__(self, max_spans: int = 5000) -> None:
        self.max_spans = max_spans
        self._lock = threading.Lock()
        self._traces: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._sessions: dict[str, OrderedDict[str, None]] = {}
        self._trace_sessions: dict[str, set[str]] = {}
        self._count = 0

    # --- SpanProcessor ---
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        record = to_record(span)
        tid = record["trace_id"]
        sid = record["attributes"].get(SESSION_ATTR)
        with self._lock:
            self._traces.setdefault(tid, []).append(record)
            self._traces.move_to_end(tid)
            self._count += 1
            if isinstance(sid, str):
                self._sessions.setdefault(sid, OrderedDict())[tid] = None
                self._trace_sessions.setdefault(tid, set()).add(sid)
            while self._count > self.max_spans and len(self._traces) > 1:
                self._evict_oldest()

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def _evict_oldest(self) -> None:
        tid, spans = self._traces.popitem(last=False)
        self._count -= len(spans)
        for sid in self._trace_sessions.pop(tid, ()):
            ids = self._sessions.get(sid)
            if ids is not None:
                ids.pop(tid, None)
                if not ids:
                    del self._sessions[sid]

    # --- чтение ---
    def spans(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._traces.get(trace_id, ()))

    def trace_ids(self, session_id: str | None = None) -> list[str]:
        with self._lock:
            if session_id is None:
                return list(self._traces)
            return list(self._sessions.get(session_id, ()))
