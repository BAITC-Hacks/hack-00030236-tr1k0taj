"""Ограниченное in-process хранилище завершённых span'ов для панели трассировки.

SpanStore — это SpanProcessor: SDK вызывает on_start/on_end из любого потока, поэтому всё под
lock'ом. Храним не больше max_spans span'ов; при переполнении выбрасываем трассы, которые дольше
всех не получали новых span'ов. Буфер нужен без БД и для span'ов, ещё не сброшенных в
`trace_spans`; `sink` (SpanWriter) получает каждую запись для записи в БД.

Привязка к сессии при записи (best effort): `session.id`/`turn.id` любого span'а трассы
запоминаются по trace_id уже в on_start, поэтому дочерние span'ы, закрывающиеся раньше `turn`,
тоже получают сессию. Остальное добирает чтение (`views.attribute_sessions`).
"""

import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from app.tracer.redact import redact

SESSION_ATTR = "session.id"
TURN_ATTR = "turn.id"
_MAX_HINTS = 10000


def iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).isoformat()


def as_turn(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _plain(attributes) -> dict[str, Any]:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in redact(attributes).items()}


def to_record(span: ReadableSpan) -> dict[str, Any]:
    """ReadableSpan → dict в форме, близкой к OTLP (идентификаторы в hex), ПД замаскированы."""
    ctx = span.context
    start, end = span.start_time, span.end_time
    attributes = _plain(span.attributes)
    sid = attributes.get(SESSION_ATTR)
    return {
        "trace_id": f"{ctx.trace_id:032x}",
        "span_id": f"{ctx.span_id:016x}",
        "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
        "session_id": str(sid) if sid is not None else None,
        "turn_id": as_turn(attributes.get(TURN_ATTR)),
        "name": span.name,
        "kind": span.kind.name.lower(),
        "start_ns": start,
        "end_ns": end,
        "start_time": iso(start),
        "end_time": iso(end),
        "duration_ms": round((end - start) / 1e6, 3) if start and end else None,
        "status": span.status.status_code.name.lower(),
        "status_message": span.status.description,
        "attributes": attributes,
        "events": [
            {"name": e.name, "time": iso(e.timestamp), "attributes": _plain(e.attributes)}
            for e in span.events
        ],
        "links": [
            {"trace_id": f"{link.context.trace_id:032x}", "span_id": f"{link.context.span_id:016x}"}
            for link in span.links
        ],
        "resource": _plain(span.resource.attributes) if span.resource else {},
    }


class SpanStore(SpanProcessor):
    def __init__(self, max_spans: int = 5000) -> None:
        self.max_spans = max_spans
        self.sink: Callable[[dict[str, Any]], None] | None = None
        self._lock = threading.Lock()
        self._traces: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._sessions: dict[str, OrderedDict[str, None]] = {}
        self._trace_sessions: dict[str, set[str]] = {}
        self._hints: OrderedDict[str, tuple[str | None, int | None]] = OrderedDict()
        self._count = 0

    def _hint(self, tid: str, sid: str | None, turn: int | None) -> tuple[str | None, int | None]:
        """Запомнить/дополнить сессию и ход трассы; вернуть известные значения. Под lock'ом."""
        old_sid, old_turn = self._hints.get(tid, (None, None))
        new = (old_sid or sid, old_turn if old_turn is not None else turn)
        if new != (None, None):
            self._hints[tid] = new
            self._hints.move_to_end(tid)
            while len(self._hints) > _MAX_HINTS:
                self._hints.popitem(last=False)
        return new

    # --- SpanProcessor ---
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        attrs = span.attributes or {}
        sid, turn = attrs.get(SESSION_ATTR), as_turn(attrs.get(TURN_ATTR))
        if sid is None and turn is None:
            return
        with self._lock:
            self._hint(f"{span.context.trace_id:032x}", str(sid) if sid else None, turn)

    def on_end(self, span: ReadableSpan) -> None:
        record = to_record(span)
        tid = record["trace_id"]
        with self._lock:
            sid, turn = self._hint(tid, record["session_id"], record["turn_id"])
            record["session_id"] = record["session_id"] or sid
            if record["turn_id"] is None:
                record["turn_id"] = turn
            self._traces.setdefault(tid, []).append(record)
            self._traces.move_to_end(tid)
            self._count += 1
            if sid:
                self._sessions.setdefault(sid, OrderedDict())[tid] = None
                self._trace_sessions.setdefault(tid, set()).add(sid)
            while self._count > self.max_spans and len(self._traces) > 1:
                self._evict_oldest()
            sink = self.sink
        if sink is not None:
            sink(record)

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

    def records(self) -> list[dict[str, Any]]:
        """Все span'ы буфера (копия списка)."""
        with self._lock:
            return [r for spans in self._traces.values() for r in spans]

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()
            self._sessions.clear()
            self._trace_sessions.clear()
            self._hints.clear()
            self._count = 0
