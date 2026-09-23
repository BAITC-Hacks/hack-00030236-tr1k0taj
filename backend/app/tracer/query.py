"""Чтение span'ов: `trace_spans` (если запись включена) + ещё не сброшенный буфер памяти.

Ошибка БД не роняет API: логируем и отвечаем тем, что есть в памяти.
"""

import logging
from typing import Any

from sqlalchemy import func, select

from app.tracer.models import trace_spans
from app.tracer.persist import get_writer
from app.tracer.setup import get_store
from app.tracer.sql import SKIP_OPTION, suppress_sql_spans
from app.tracer.store import iso
from app.tracer.views import Record, attribute_sessions, by_trace, merge

log = logging.getLogger(__name__)


def _memory() -> list[Record]:
    store = get_store()
    return attribute_sessions(store.records()) if store else []


def _from_row(row: Any) -> Record:
    r = dict(row)
    r["start_time"], r["end_time"] = iso(r["start_ns"]), iso(r["end_ns"])
    return r


async def _db(stmt) -> list[Any] | None:
    writer = get_writer()
    if writer is None:
        return None
    try:
        with suppress_sql_spans():
            async with writer.session_factory() as db:
                result = await db.execute(stmt.execution_options(**{SKIP_OPTION: True}))
                return list(result.mappings())
    except Exception:
        log.warning("tracer: trace_spans read failed, using memory only", exc_info=True)
        return None


def _session_traces(sids):
    return select(trace_spans.c.trace_id).where(trace_spans.c.session_id.in_(sids))


async def trace_records(trace_id: str) -> list[Record]:
    rows = await _db(select(trace_spans).where(trace_spans.c.trace_id == trace_id)) or []
    memory = [r for r in _memory() if r["trace_id"] == trace_id]
    return merge([_from_row(r) for r in rows], memory)


async def session_records(session_ids: list[str]) -> list[Record]:
    """Все span'ы трасс, где хоть один span принадлежит одной из сессий."""
    if not session_ids:
        return []
    stmt = select(trace_spans).where(trace_spans.c.trace_id.in_(_session_traces(session_ids)))
    rows = await _db(stmt) or []
    memory = _memory()
    tids = {r["trace_id"] for r in memory if r.get("session_id") in session_ids}
    return [r for r in merge([_from_row(r) for r in rows],
                             [r for r in memory if r["trace_id"] in tids])
            if r.get("session_id") in session_ids]


async def recent_sessions(limit: int, offset: int) -> list[str]:
    """Сессии по последней активности, новые первыми."""
    last: dict[str, int] = {}
    rows = await _db(
        select(trace_spans.c.session_id,
               func.max(func.coalesce(trace_spans.c.end_ns, trace_spans.c.start_ns)).label("last"))
        .where(trace_spans.c.session_id.is_not(None))
        .group_by(trace_spans.c.session_id)
        .order_by(func.max(func.coalesce(trace_spans.c.end_ns, trace_spans.c.start_ns)).desc())
        .limit(limit + offset)
    ) or []
    for row in rows:
        last[row["session_id"]] = row["last"] or 0
    for r in _memory():
        if sid := r.get("session_id"):
            last[sid] = max(last.get(sid, 0), r["end_ns"] or r["start_ns"] or 0)
    ordered = sorted(last, key=lambda s: last[s], reverse=True)
    return ordered[offset:offset + limit]


async def recent_traces(session_id: str | None, limit: int) -> dict[str, list[Record]]:
    if session_id is not None:
        return dict(by_trace(await session_records([session_id])))
    first = func.min(trace_spans.c.start_ns)
    rows = await _db(select(trace_spans.c.trace_id).group_by(trace_spans.c.trace_id)
                     .order_by(first.desc()).limit(limit)) or []
    tids = [row["trace_id"] for row in rows]
    db_rows = (await _db(select(trace_spans).where(trace_spans.c.trace_id.in_(tids))) or []
               if tids else [])
    # вызывающий сортирует и режет по limit; буфер памяти ограничен buffer_spans
    return dict(by_trace(merge([_from_row(r) for r in db_rows], _memory())))
