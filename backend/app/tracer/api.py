"""HTTP трассировки для панели: дерево span'ов одной трассы и список последних трасс."""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.tracer.setup import get_store
from app.tracer.store import SESSION_ATTR, TURN_ATTR

router = APIRouter(tags=["trace"])


class SpanEventView(BaseModel):
    name: str = Field(description="Имя события: `exception`, `cancelled`, `playback`, ...")
    time: str | None = Field(description="Время события, ISO 8601 UTC")
    attributes: dict[str, Any] = Field(description="Атрибуты события (ПД замаскированы)")


class SpanLinkView(BaseModel):
    trace_id: str = Field(description="Трасса связанного span'а (32 hex)")
    span_id: str = Field(description="Связанный span (16 hex), например turn для `agent.run`")


class SpanView(BaseModel):
    trace_id: str = Field(description="ID трассы, 32 hex (W3C trace-id)")
    span_id: str = Field(description="ID span'а, 16 hex")
    parent_span_id: str | None = Field(
        description="Родитель; для корня может указывать на span браузера (нет в хранилище)"
    )
    name: str = Field(description="Имя этапа: `POST /calls/{session_id}/turns/text`, `turn`, "
                      "`router`, `tts.sentence`, ...")
    kind: str = Field(description="server | internal | client | producer | consumer")
    start_time: str | None = Field(description="Начало, ISO 8601 UTC")
    end_time: str | None = Field(description="Конец, ISO 8601 UTC")
    duration_ms: float | None = Field(description="Длительность span'а, мс")
    status: str = Field(description="unset | ok | error")
    status_message: str | None = Field(default=None, description="Описание ошибки")
    attributes: dict[str, Any] = Field(description="Атрибуты span'а (ПД замаскированы)")
    events: list[SpanEventView] = Field(description="События внутри span'а")
    links: list[SpanLinkView] = Field(description="Связи с span'ами вне дерева")
    children: list["SpanView"] = Field(default_factory=list, description="Дочерние span'ы")


class TraceSummary(BaseModel):
    trace_id: str = Field(description="ID трассы, 32 hex")
    root_name: str = Field(description="Имя корневого span'а (обычно HTTP-запрос)")
    start_time: str | None = Field(description="Начало самого раннего span'а, ISO 8601 UTC")
    duration_ms: float | None = Field(description="От первого начала до последнего конца, мс")
    span_count: int = Field(description="Сколько span'ов трассы в хранилище")
    session_id: str | None = Field(description="`session.id` из любого span'а трассы")
    turn_id: str | None = Field(description="`turn.id` из любого span'а трассы")
    error: bool = Field(description="Есть ли span со статусом error")


class TraceView(TraceSummary):
    spans: list[SpanView] = Field(description="Корневые span'ы с вложенными children")


def _summary(trace_id: str, records: list[dict[str, Any]]) -> TraceSummary:
    ids = {r["span_id"] for r in records}
    roots = [r for r in records if r["parent_span_id"] not in ids]
    root = min(roots or records, key=lambda r: r["start_ns"] or 0)
    starts = [r["start_ns"] for r in records if r["start_ns"]]
    ends = [r["end_ns"] for r in records if r["end_ns"]]
    first = min(records, key=lambda r: r["start_ns"] or 0)

    def attr(key: str) -> str | None:
        value = next((r["attributes"][key] for r in records if key in r["attributes"]), None)
        return None if value is None else str(value)

    return TraceSummary(
        trace_id=trace_id, root_name=root["name"], start_time=first["start_time"],
        duration_ms=round((max(ends) - min(starts)) / 1e6, 3) if starts and ends else None,
        span_count=len(records), session_id=attr(SESSION_ATTR), turn_id=attr(TURN_ATTR),
        error=any(r["status"] == "error" for r in records),
    )


def build_trace(trace_id: str, records: list[dict[str, Any]]) -> TraceView:
    ordered = sorted(records, key=lambda r: r["start_ns"] or 0)
    views = {r["span_id"]: SpanView(**r) for r in ordered}
    roots: list[SpanView] = []
    for r in ordered:
        parent = views.get(r["parent_span_id"] or "")
        (parent.children if parent else roots).append(views[r["span_id"]])
    return TraceView(**_summary(trace_id, records).model_dump(), spans=roots)


TraceId = Annotated[str, Path(pattern="^[0-9a-fA-F]{32}$",
                              description="W3C trace-id (заголовок ответа `x-trace-id`)")]


@router.get("/traces/{trace_id}", summary="Трасса хода как дерево span'ов")
async def get_trace(trace_id: TraceId) -> TraceView:
    store = get_store()
    records = store.spans(trace_id.lower()) if store else []
    if not records:
        raise HTTPException(404, "trace not found")
    return build_trace(trace_id.lower(), records)


@router.get("/traces", summary="Последние трассы (новые первыми)")
async def list_traces(
    session_id: Annotated[str | None, Query(description="UUID сессии (`session.id`)")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Сколько трасс вернуть")] = 20,
) -> list[TraceSummary]:
    store = get_store()
    if store is None:
        return []
    summaries = [_summary(tid, spans) for tid in store.trace_ids(session_id)
                 if (spans := store.spans(tid))]
    summaries.sort(key=lambda s: s.start_time or "", reverse=True)
    return summaries[:limit]
