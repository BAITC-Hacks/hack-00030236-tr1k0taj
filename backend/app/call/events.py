"""События SSE одного хода (docs/specs/call-api.md). В data всегда есть type и turn_id."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.context import Fact
from app.router import Alternative, ScenarioPick


class _Event(BaseModel):
    turn_id: int = Field(description="0, если ход ещё не зарегистрирован (ошибка STT)")


class TranscriptEvent(_Event):
    type: Literal["transcript"] = "transcript"
    text: str
    language: str | None
    source: Literal["stt", "text"]


class TurnStartedEvent(_Event):
    type: Literal["turn.started"] = "turn.started"
    session_id: str
    generation: int
    context_version: int


class RoutingEvent(_Event):
    type: Literal["routing"] = "routing"
    decision: Literal["route", "clarify", "handoff"]
    scenarios: list[ScenarioPick] = Field(description="Порядок обслуживания: urgent первыми")
    alternatives: list[Alternative]
    language: str
    slots: dict[str, Any]
    is_continuation: bool
    clarify_options: list[str] = []


class ActionEvent(_Event):
    type: Literal["action"] = "action"
    name: str
    mode: Literal["read", "preview", "execute", "handoff", "unsupported"]
    params: dict[str, Any]
    ok: bool
    result: Any = None
    error: dict[str, str] | None = None


class FactsEvent(_Event):
    type: Literal["facts"] = "facts"
    facts: list[Fact]


class ReplyDeltaEvent(_Event):
    type: Literal["reply.delta"] = "reply.delta"
    text: str


class ReplyDoneEvent(_Event):
    type: Literal["reply.done"] = "reply.done"
    text: str
    language: str


class AudioEvent(_Event):
    type: Literal["audio"] = "audio"
    seq: int
    mime: str
    data: str = Field(description="Аудио предложения, base64")
    text: str = Field(description="Озвученное предложение")


class ErrorEvent(_Event):
    type: Literal["error"] = "error"
    stage: Literal["stt", "router", "executor", "responder", "tts", "turn"]
    code: str = Field(examples=["stt_unavailable", "llm_unavailable", "router_invalid"])
    message: str
    fatal: bool = Field(description="true — ход прерван, дальше придёт только turn.done")


class TurnCancelledEvent(_Event):
    type: Literal["turn.cancelled"] = "turn.cancelled"


class Latency(BaseModel):
    """Длительности этапов, мс. null — этап не выполнялся. Параллельные ветки не складываются."""

    stt: int | None = None
    router: int | None = None
    reads: int | None = None
    response_first_token: int | None = None
    response: int | None = None
    tts_first_audio: int | None = None
    total: int | None = Field(None, description="Приём запроса → первое аудио (или reply.done)")


class TurnDoneEvent(_Event):
    """Трассировка хода в формате README кита."""

    type: Literal["turn.done"] = "turn.done"
    transcript: str
    language: str | None
    scenarios: list[ScenarioPick]
    alternatives: list[Alternative]
    reason: str
    slots: dict[str, Any]
    actions: list[str] = Field(examples=[["find_client", "get_claim:read"]])
    reply: str
    latency_ms: Latency
    context_version: int


TurnEvent = Annotated[
    TranscriptEvent
    | TurnStartedEvent
    | RoutingEvent
    | ActionEvent
    | FactsEvent
    | ReplyDeltaEvent
    | ReplyDoneEvent
    | AudioEvent
    | ErrorEvent
    | TurnCancelledEvent
    | TurnDoneEvent,
    Field(discriminator="type"),
]


def sse(event: BaseModel) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
