"""CLIENT span'ы `db.query` для SQL через события SQLAlchemy (без auto-instrumentation пакета).

В атрибуты идёт только текст запроса с плейсхолдерами — параметры не пишем (в них ПД).
Собственные запросы tracer'а (сброс и чтение `trace_spans`) помечены execution option
`SKIP_OPTION` или выполняются внутри `suppress_sql_spans()` — иначе запись трасс порождала бы
новые span'ы (петля).
"""

import re
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

SKIP_OPTION = "tracer_skip"
_MAX_TEXT = 2000
_KEY = "_tracer_span"
_suppressed: ContextVar[bool] = ContextVar("tracer_sql_suppressed", default=False)
_instrumented: "weakref.WeakSet[Any]" = weakref.WeakSet()
_COLLECTION = re.compile(r'\b(?:from|into|update|join|table)\s+"?([A-Za-z_][\w.]*)', re.IGNORECASE)


@contextmanager
def suppress_sql_spans() -> Iterator[None]:
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


def _attributes(statement: str) -> dict[str, Any]:
    text = " ".join(statement.split())
    attrs: dict[str, Any] = {"db.system.name": "postgresql", "db.query.text": text[:_MAX_TEXT]}
    if words := text.split(" ", 1)[0]:
        attrs["db.operation.name"] = words.upper()
    if match := _COLLECTION.search(text):
        attrs["db.collection.name"] = match.group(1)
    return attrs


def instrument_sqlalchemy(engine: Any, *, only_with_parent: bool = True) -> None:
    """Повесить span'ы `db.query` на Engine/AsyncEngine. Идемпотентно.

    only_with_parent=True: запросы вне какого-либо span'а (фоновые задачи без трассы, старт)
    span'ов не создают — не плодим одиночные трассы.
    """
    from sqlalchemy import event

    sync_engine = getattr(engine, "sync_engine", engine)
    if sync_engine in _instrumented:
        return
    _instrumented.add(sync_engine)
    tracer = trace.get_tracer("voice-router.sql")

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        if context is None or _suppressed.get() or context.execution_options.get(SKIP_OPTION):
            return
        if only_with_parent and not trace.get_current_span().get_span_context().is_valid:
            return
        attrs = _attributes(statement)
        if executemany:
            attrs["db.operation.batch.size"] = len(parameters or ())
        setattr(context, _KEY, tracer.start_span("db.query", kind=SpanKind.CLIENT,
                                                 attributes=attrs))

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        span = getattr(context, _KEY, None)
        if span is None:
            return
        setattr(context, _KEY, None)
        rows = getattr(cursor, "rowcount", -1)
        if isinstance(rows, int) and rows >= 0:
            span.set_attribute("db.response.returned_rows", rows)
        span.end()

    @event.listens_for(sync_engine, "handle_error")
    def _error(exception_context):
        context = exception_context.execution_context
        span = getattr(context, _KEY, None) if context is not None else None
        if span is None:
            return
        setattr(context, _KEY, None)
        exc = exception_context.original_exception
        span.record_exception(exc)
        span.set_attribute("error.type", type(exc).__name__)
        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
        span.end()
