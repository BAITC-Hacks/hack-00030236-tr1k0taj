"""HTTP трассировки: сквозная трасса звонка по session UUID, история звонков, дерево трассы.

Читает `trace_spans`, если запись включена (`start_persistence`), плюс ещё не сброшенный буфер
памяти; без БД — только буфер. Маршруты `/traces/sessions*` объявлены раньше `/traces/{trace_id}`.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from app.tracer import query
from app.tracer.views import (
    SessionTrace,
    SessionTraceSummary,
    TraceSummary,
    TraceView,
    build_session,
    build_trace,
    trace_summary,
)

router = APIRouter(tags=["trace"])

SessionId = Annotated[str, Path(description="UUID сессии звонка (`session.id`)")]
TraceId = Annotated[str, Path(pattern="^[0-9a-fA-F]{32}$",
                              description="W3C trace-id (заголовок ответа `x-trace-id`)")]


@router.get(
    "/traces/sessions",
    summary="История звонков",
    description="Сводка по каждому звонку (сессии): время, ходы, ошибки, LLM-вызовы, токены по "
                "моделям, последняя реплика, сценарии. Новые (по последней активности) первыми.",
)
async def list_sessions(
    limit: Annotated[int, Query(ge=1, le=200, description="Сколько звонков вернуть")] = 50,
    offset: Annotated[int, Query(ge=0, description="Сколько пропустить (пагинация)")] = 0,
) -> list[SessionTraceSummary]:
    sids = await query.recent_sessions(limit, offset)
    records = await query.session_records(sids)
    grouped: dict[str, list] = {sid: [] for sid in sids}
    for r in records:
        grouped[r["session_id"]].append(r)
    return [build_session(sid, rs).summary for sid, rs in grouped.items() if rs]


@router.get(
    "/traces/sessions/{session_id}",
    summary="Сквозная трасса звонка",
    description="Все трассы сессии (ходы, cancel, playback, фоновые `agent.run`) и разбор каждого "
                "хода: реплика, источник ввода, решение роутера, длительности этапов, токены по "
                "моделям, ошибки. Span'ы без `session.id` привязываются по своей трассе. "
                "Полное дерево хода — `GET /traces/{trace_id}`.",
    responses={404: {"description": "Сессия не найдена"}},
)
async def get_session(session_id: SessionId) -> SessionTrace:
    records = await query.session_records([session_id])
    if not records:
        raise HTTPException(404, "session not found")
    return build_session(session_id, records)


@router.get("/traces/{trace_id}", summary="Трасса хода как дерево span'ов",
            responses={404: {"description": "Трасса не найдена"}})
async def get_trace(trace_id: TraceId) -> TraceView:
    records = await query.trace_records(trace_id.lower())
    if not records:
        raise HTTPException(404, "trace not found")
    return build_trace(trace_id.lower(), records)


@router.get("/traces", summary="Последние трассы (новые первыми)")
async def list_traces(
    session_id: Annotated[str | None, Query(description="UUID сессии (`session.id`)")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Сколько трасс вернуть")] = 20,
) -> list[TraceSummary]:
    traces = await query.recent_traces(session_id, limit)
    summaries = [trace_summary(tid, spans) for tid, spans in traces.items() if spans]
    summaries.sort(key=lambda s: s.start_time or "", reverse=True)
    return summaries[:limit]

