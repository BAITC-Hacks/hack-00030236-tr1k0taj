"""Запись span'ов в `trace_spans`: очередь без блокировок + фоновый asyncio-сбросчик.

SpanStore.on_end (любой поток) → SpanWriter.enqueue: запись кладётся в ограниченную очередь,
при переполнении выбрасывается и считается в `dropped` — звонок трассировка не тормозит.
Сбросчик раз в `interval` секунд или при `batch_size` записях вставляет пачку
(`ON CONFLICT DO NOTHING`) и дописывает `session_id`/`turn_id` span'ам той же трассы, закрытым
раньше span'а с `session.id`. Ошибки БД логируются и считаются в `failed`, звонок не роняют.
"""

import asyncio
import logging
import threading
from collections import deque
from typing import Any

from sqlalchemy import bindparam, func, update
from sqlalchemy.dialects.postgresql import insert

from app.tracer.models import COLUMNS, trace_spans
from app.tracer.setup import get_store, setup_tracing
from app.tracer.sql import SKIP_OPTION, suppress_sql_spans

log = logging.getLogger(__name__)

_BACKFILL = (
    update(trace_spans)
    .where(trace_spans.c.trace_id == bindparam("tid"))
    .where((trace_spans.c.session_id.is_(None)) | (trace_spans.c.turn_id.is_(None)))
    .values(session_id=func.coalesce(trace_spans.c.session_id, bindparam("sid")),
            turn_id=func.coalesce(trace_spans.c.turn_id, bindparam("turn")))
)


class SpanWriter:
    def __init__(self, session_factory: Any, *, max_queue: int = 10000, batch_size: int = 200,
                 interval: float = 0.3) -> None:
        self.session_factory = session_factory
        self.max_queue, self.batch_size, self.interval = max_queue, batch_size, interval
        self.stats = {"queued": 0, "flushed": 0, "dropped": 0, "failed": 0}
        self._pending: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._flush_lock: asyncio.Lock | None = None

    # --- любой поток ---
    def enqueue(self, record: dict[str, Any]) -> None:
        with self._lock:
            if len(self._pending) >= self.max_queue:
                self.stats["dropped"] += 1
                return
            self._pending.append(record)
            self.stats["queued"] += 1
            full = len(self._pending) == self.batch_size
        if full and self._loop is not None and self._wake is not None:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:  # цикл закрыт — заберёт stop()/следующий flush
                pass

    # --- цикл событий ---
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake, self._flush_lock = asyncio.Event(), asyncio.Lock()
        self._task = asyncio.create_task(self._run(), name="tracer-flush")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()
        self._loop = None

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self.interval)
            except TimeoutError:
                pass
            self._wake.clear()
            await self.flush()

    async def flush(self) -> int:
        """Сбросить всё накопленное; вернуть число вставленных записей."""
        lock = self._flush_lock or asyncio.Lock()
        async with lock:
            total = 0
            while True:
                with self._lock:
                    batch = [self._pending.popleft()
                             for _ in range(min(self.batch_size, len(self._pending)))]
                if not batch:
                    return total
                try:
                    await self._insert(batch)
                    self.stats["flushed"] += len(batch)
                    total += len(batch)
                except Exception:
                    self.stats["failed"] += len(batch)
                    log.warning("tracer: failed to persist %d spans", len(batch), exc_info=True)

    async def _insert(self, batch: list[dict[str, Any]]) -> None:
        rows = [{c: r.get(c) for c in COLUMNS} for r in batch]
        for row in rows:
            for key in ("attributes", "resource"):
                row[key] = row[key] or {}
            for key in ("events", "links"):
                row[key] = row[key] or []
        known: dict[str, tuple[str | None, int | None]] = {}
        for r in batch:
            sid, turn = known.get(r["trace_id"], (None, None))
            known[r["trace_id"]] = (sid or r.get("session_id"),
                                    turn if turn is not None else r.get("turn_id"))
        backfill = [{"tid": tid, "sid": sid, "turn": turn}
                    for tid, (sid, turn) in known.items() if sid or turn is not None]
        with suppress_sql_spans():
            async with self.session_factory() as db:
                stmt = insert(trace_spans).on_conflict_do_nothing(index_elements=["span_id"])
                await db.execute(stmt.execution_options(**{SKIP_OPTION: True}), rows)
                if backfill:
                    await db.execute(_BACKFILL.execution_options(**{SKIP_OPTION: True}), backfill)
                await db.commit()


_writer: SpanWriter | None = None


def get_writer() -> SpanWriter | None:
    return _writer


async def start_persistence(session_factory: Any, **options: Any) -> SpanWriter:
    """Включить запись span'ов в БД (из lifespan). `session_factory` — async_sessionmaker."""
    global _writer
    if _writer is not None:
        return _writer
    setup_tracing()
    store = get_store()
    assert store is not None
    writer = SpanWriter(session_factory, **options)
    await writer.start()
    store.sink = writer.enqueue
    _writer = writer
    return writer


async def stop_persistence() -> None:
    """Отключить запись и сбросить остаток очереди (в finally lifespan, до engine.dispose())."""
    global _writer
    writer, _writer = _writer, None
    store = get_store()
    if store is not None and writer is not None and store.sink == writer.enqueue:
        store.sink = None
    if writer is not None:
        await writer.stop()


async def flush_traces() -> int:
    """Сбросить очередь в БД сейчас (тесты, ручная проверка). Без записи — 0."""
    return await _writer.flush() if _writer is not None else 0


def persistence_stats() -> dict[str, int] | None:
    return dict(_writer.stats) if _writer is not None else None
