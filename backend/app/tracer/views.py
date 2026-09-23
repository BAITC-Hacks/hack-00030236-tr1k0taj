"""Ответы API трассировки и их сборка из записей span'ов (dict из store/БД)."""

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from app.tracer.store import SESSION_ATTR, TURN_ATTR, as_turn, iso

Record = dict[str, Any]


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
    session_id: str | None = Field(
        default=None, description="Сессия звонка: свой `session.id` или унаследованный от трассы")
    attributes: dict[str, Any] = Field(
        description="Атрибуты span'а (ПД замаскированы; `local.*` — только локально)")
    events: list[SpanEventView] = Field(description="События внутри span'а")
    links: list[SpanLinkView] = Field(description="Связи с span'ами вне дерева")
    resource: dict[str, Any] = Field(default_factory=dict,
                                     description="Ресурс OTel: `service.name` и т.п.")
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


class TokenTotals(BaseModel):
    input: int = Field(0, description="Сумма `gen_ai.usage.input_tokens`")
    output: int = Field(0, description="Сумма `gen_ai.usage.output_tokens`")
    total: int = Field(0, description="input + output")


class ModelUsage(BaseModel):
    input: int = Field(0, description="Входные токены на этой модели")
    output: int = Field(0, description="Выходные токены на этой модели")
    calls: int = Field(0, description="Сколько LLM-вызовов с usage")


class ErrorView(BaseModel):
    trace_id: str = Field(description="Трасса с ошибкой")
    span_id: str = Field(description="Span с ошибкой")
    span_name: str = Field(description="Имя span'а")
    code: str | None = Field(description="`error.code`, если задан")
    stage: str | None = Field(description="`error.stage`, если задан")
    message: str | None = Field(description="Описание статуса ERROR")
    time: str | None = Field(description="Конец span'а, ISO 8601 UTC")


class RouterView(BaseModel):
    decision: str | None = Field(description="`router.decision`: route | clarify | handoff | ...")
    scenario_id: str | None = Field(description="`router.scenario_id`")
    confidence: float | None = Field(description="`router.confidence`")


class TurnTrace(BaseModel):
    turn_id: int | None = Field(description="`turn.id` хода")
    trace_id: str = Field(description="Трасса хода (`GET /traces/{trace_id}` — полное дерево)")
    started_at: str | None = Field(description="Начало span'а `turn`, ISO 8601 UTC")
    duration_ms: float | None = Field(description="Длительность span'а `turn`, мс")
    transcript: str | None = Field(description="Распознанная реплика (`local.transcript`)")
    input_source: str | None = Field(description="`input.source`: text | audio | ...")
    router: RouterView = Field(description="Решение роутера")
    stages: dict[str, float] = Field(
        description="Этап → мс по прямым детям `turn` (stt, router, executor, responder, tts...); "
                    "повторы одного этапа — от первого начала до последнего конца, не сумма")
    tokens_by_model: dict[str, ModelUsage] = Field(description="Токены хода по моделям")
    errors: list[ErrorView] = Field(description="Ошибки в трассе хода")


class SessionTraceSummary(BaseModel):
    session_id: str = Field(description="UUID сессии звонка")
    first_seen: str | None = Field(description="Первый span сессии, ISO 8601 UTC")
    last_seen: str | None = Field(description="Последний конец span'а сессии, ISO 8601 UTC")
    duration_ms: float | None = Field(description="last_seen − first_seen, мс")
    traces: int = Field(description="Число трасс (ходы, cancel, playback, agent.run...)")
    turns: int = Field(description="Число span'ов `turn`")
    errors: int = Field(description="Число span'ов с ошибкой")
    llm_calls: int = Field(description="LLM-вызовов (span'ы с `gen_ai.usage.*` или `llm.call`)")
    tokens: TokenTotals = Field(description="Токены за звонок")
    tokens_by_model: dict[str, ModelUsage] = Field(description="Токены по моделям")
    last_transcript: str | None = Field(description="Реплика последнего хода с транскриптом")
    scenarios: list[str] = Field(description="Разные `router.scenario_id` по порядку ходов")


class SessionTrace(BaseModel):
    summary: SessionTraceSummary = Field(description="Сводка звонка")
    turns: list[TurnTrace] = Field(description="Ходы по времени")
    traces: list[TraceSummary] = Field(description="Все трассы сессии, по времени")


# --- сборка ---

def attribute_sessions(records: list[Record]) -> list[Record]:
    """Сессия/ход span'а — свои или унаследованные от любого span'а той же трассы."""
    known: dict[str, tuple[str | None, int | None]] = {}
    for r in records:
        sid = r.get("session_id") or r["attributes"].get(SESSION_ATTR)
        turn = r.get("turn_id")
        if turn is None:
            turn = as_turn(r["attributes"].get(TURN_ATTR))
        old_sid, old_turn = known.get(r["trace_id"], (None, None))
        known[r["trace_id"]] = (old_sid or (str(sid) if sid else None),
                                old_turn if old_turn is not None else turn)
    out = []
    for r in records:
        sid, turn = known[r["trace_id"]]
        out.append({**r, "session_id": r.get("session_id") or sid,
                    "turn_id": r.get("turn_id") if r.get("turn_id") is not None else turn})
    return out


def merge(*sources: list[Record]) -> list[Record]:
    """Объединить записи из БД и буфера без дублей по span_id; сессии — с наследованием."""
    seen: dict[str, Record] = {}
    for records in sources:
        for r in records:
            prev = seen.get(r["span_id"])
            seen[r["span_id"]] = r if prev is None else {
                **prev, "session_id": prev.get("session_id") or r.get("session_id"),
                "turn_id": prev["turn_id"] if prev.get("turn_id") is not None else r.get("turn_id"),
            }
    return attribute_sessions(list(seen.values()))


def by_trace(records: list[Record]) -> dict[str, list[Record]]:
    grouped: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        grouped[r["trace_id"]].append(r)
    return grouped


def trace_summary(trace_id: str, records: list[Record]) -> TraceSummary:
    ids = {r["span_id"] for r in records}
    roots = [r for r in records if r["parent_span_id"] not in ids]
    root = min(roots or records, key=lambda r: r["start_ns"] or 0)
    starts = [r["start_ns"] for r in records if r["start_ns"]]
    ends = [r["end_ns"] for r in records if r["end_ns"]]
    first = min(records, key=lambda r: r["start_ns"] or 0)
    sid = next((r["session_id"] for r in records if r.get("session_id")), None)
    turn = next((r["attributes"][TURN_ATTR] for r in records if TURN_ATTR in r["attributes"]),
                None)
    return TraceSummary(
        trace_id=trace_id, root_name=root["name"], start_time=first["start_time"],
        duration_ms=round((max(ends) - min(starts)) / 1e6, 3) if starts and ends else None,
        span_count=len(records), session_id=sid, turn_id=None if turn is None else str(turn),
        error=any(r["status"] == "error" for r in records),
    )


def build_trace(trace_id: str, records: list[Record]) -> TraceView:
    ordered = sorted(records, key=lambda r: r["start_ns"] or 0)
    views = {r["span_id"]: SpanView(**r) for r in ordered}
    roots: list[SpanView] = []
    for r in ordered:
        parent = views.get(r["parent_span_id"] or "")
        (parent.children if parent else roots).append(views[r["span_id"]])
    return TraceView(**trace_summary(trace_id, records).model_dump(), spans=roots)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage(records: list[Record]) -> tuple[dict[str, ModelUsage], int]:
    """Токены по моделям и число LLM-вызовов. Usage родителя не считаем, если он есть у потомка
    (например, `router` и вложенный `llm.call`) — иначе двойной счёт."""
    has_usage = [r for r in records if "gen_ai.usage.input_tokens" in r["attributes"]
                 or "gen_ai.usage.output_tokens" in r["attributes"]]
    parents = {r["span_id"]: r["parent_span_id"] for r in records}
    shadowed: set[str] = set()
    for r in has_usage:
        p = parents.get(r["span_id"])
        while p and p not in shadowed:
            shadowed.add(p)
            p = parents.get(p)
    models: dict[str, ModelUsage] = {}
    counted = set()
    for r in has_usage:
        if r["span_id"] in shadowed:
            continue
        a = r["attributes"]
        model = str(a.get("gen_ai.response.model") or a.get("gen_ai.request.model") or "unknown")
        m = models.setdefault(model, ModelUsage())
        m.input += _int(a.get("gen_ai.usage.input_tokens"))
        m.output += _int(a.get("gen_ai.usage.output_tokens"))
        m.calls += 1
        counted.add(r["span_id"])
    calls = len(counted) + sum(1 for r in records
                               if r["name"] == "llm.call" and r["span_id"] not in counted
                               and r["span_id"] not in shadowed)
    return models, calls


def _errors(records: list[Record]) -> list[ErrorView]:
    return [
        ErrorView(trace_id=r["trace_id"], span_id=r["span_id"], span_name=r["name"],
                  code=_str(r["attributes"].get("error.code")),
                  stage=_str(r["attributes"].get("error.stage")),
                  message=r.get("status_message"), time=r["end_time"])
        for r in sorted(records, key=lambda r: r["start_ns"] or 0)
        if r["status"] == "error" or "error.code" in r["attributes"]
    ]


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _first(records: list[Record], key: str, prefer: str | None = None) -> Any:
    ordered = sorted(records, key=lambda r: (r["name"] != prefer, r["start_ns"] or 0))
    return next((r["attributes"][key] for r in ordered if key in r["attributes"]), None)


def _turn(turn: Record, records: list[Record]) -> TurnTrace:
    children = [r for r in records if r["parent_span_id"] == turn["span_id"]]
    windows: dict[str, list[int]] = {}
    for c in children:
        if c["start_ns"] and c["end_ns"]:
            w = windows.setdefault(c["name"], [c["start_ns"], c["end_ns"]])
            w[0], w[1] = min(w[0], c["start_ns"]), max(w[1], c["end_ns"])
    confidence = _first(records, "router.confidence", "router")
    models, _ = _usage(records)
    return TurnTrace(
        turn_id=as_turn(turn["attributes"].get(TURN_ATTR)) if turn.get("turn_id") is None
        else turn["turn_id"],
        trace_id=turn["trace_id"], started_at=turn["start_time"], duration_ms=turn["duration_ms"],
        transcript=_str(_first(records, "local.transcript", "stt")),
        input_source=_str(_first(records, "input.source", "turn")),
        router=RouterView(
            decision=_str(_first(records, "router.decision", "router")),
            scenario_id=_str(_first(records, "router.scenario_id", "router")),
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        ),
        stages={name: round((e - s) / 1e6, 3) for name, (s, e) in windows.items()},
        tokens_by_model=models, errors=_errors(records),
    )


def build_session(session_id: str, records: list[Record]) -> SessionTrace:
    """records — span'ы сессии (после attribute_sessions)."""
    traces = by_trace(records)
    turns = [_turn(t, traces[t["trace_id"]])
             for t in sorted(records, key=lambda r: r["start_ns"] or 0) if t["name"] == "turn"]
    models, calls = _usage(records)
    starts = [r["start_ns"] for r in records if r["start_ns"]]
    ends = [r["end_ns"] or r["start_ns"] for r in records if r["end_ns"] or r["start_ns"]]
    tokens_in = sum(m.input for m in models.values())
    tokens_out = sum(m.output for m in models.values())
    scenarios: list[str] = []
    for t in turns:
        if t.router.scenario_id and t.router.scenario_id not in scenarios:
            scenarios.append(t.router.scenario_id)
    summary = SessionTraceSummary(
        session_id=session_id,
        first_seen=iso(min(starts)) if starts else None,
        last_seen=iso(max(ends)) if ends else None,
        duration_ms=round((max(ends) - min(starts)) / 1e6, 3) if starts and ends else None,
        traces=len(traces), turns=len(turns),
        errors=sum(1 for r in records if r["status"] == "error" or "error.code" in r["attributes"]),
        llm_calls=calls,
        tokens=TokenTotals(input=tokens_in, output=tokens_out, total=tokens_in + tokens_out),
        tokens_by_model=models,
        last_transcript=next((t.transcript for t in reversed(turns) if t.transcript), None),
        scenarios=scenarios,
    )
    summaries = sorted((trace_summary(tid, rs) for tid, rs in traces.items()),
                       key=lambda s: s.start_time or "")
    return SessionTrace(summary=summary, turns=turns, traces=summaries)
