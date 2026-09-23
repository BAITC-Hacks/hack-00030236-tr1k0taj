"""События SSE одного хода (docs/specs/call-api.md). В data всегда есть type и turn_id."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.context import Fact
from app.router import Alternative, ScenarioPick


class _Event(BaseModel):
    turn_id: int = Field(description="0, если ход ещё не зарегистрирован (ошибка STT)")
    request_id: str | None = None
    event_seq: int | None = None


class TranscriptEvent(_Event):
    """Что сказал клиент: итог STT или текст из резервного ввода. Первое событие хода."""

    type: Literal["transcript"] = "transcript"
    text: str
    language: str | None
    source: Literal["stt", "text"]


class TurnStartedEvent(_Event):
    """Ход записан на доску; с этого момента известен turn_id (для cancel/playback)."""

    type: Literal["turn.started"] = "turn.started"
    session_id: str
    generation: int
    context_version: int


class RoutingEvent(_Event):
    """Решение LLM-роутера после валидации ID и политики порогов (route / clarify / handoff)."""

    type: Literal["routing"] = "routing"
    decision: Literal["route", "clarify", "handoff"]
    scenarios: list[ScenarioPick] = Field(description="Порядок обслуживания: urgent первыми")
    alternatives: list[Alternative]
    language: str
    slots: dict[str, Any]
    is_continuation: bool
    clarify_options: list[str] = []


class ActionEvent(_Event):
    """Действие исполнителя из actions.json: read, preview, execute, handoff, unsupported."""

    type: Literal["action"] = "action"
    name: str
    mode: Literal["read", "preview", "execute", "handoff", "unsupported"]
    params: dict[str, Any]
    ok: bool
    result: Any = None
    error: dict[str, str] | None = None


class FactsEvent(_Event):
    """Факты хода из данных. У каждого есть source и source_id."""

    type: Literal["facts"] = "facts"
    facts: list[Fact]


class ReplyDeltaEvent(_Event):
    """Очередной кусок текста ответа. Склеивайте куски по порядку."""

    type: Literal["reply.delta"] = "reply.delta"
    text: str
    response_id: str | None = None
    segment_id: str | None = None


class ReplyDoneEvent(_Event):
    """Полный текст ответа и язык, на котором он сказан."""

    type: Literal["reply.done"] = "reply.done"
    text: str
    language: str
    response_id: str | None = None


class AudioEvent(_Event):
    """Кусок озвучки. Играйте по seq (сквозной на весь ход); первое может прийти раньше reply.done.

    mime — либо `audio/mpeg` (целое предложение), либо `audio/pcm;rate=24000;channels=1`
    (потоковый PCM 16-bit mono little-endian: чанки одного предложения склеиваются подряд).
    """

    type: Literal["audio"] = "audio"
    seq: int
    mime: str
    data: str = Field(description="Аудио-чанк, base64")
    text: str = Field(description="Озвученный текст (предложение или чанк)")
    chunk: int = Field(0, description="Номер чанка внутри text (0 — первый)")
    final: bool = Field(True, description="Последний чанк для этого text")
    filler: bool = Field(False, description="Короткая фраза-заглушка, не часть ответа")
    response_id: str | None = None
    segment_id: str | None = None


class ErrorEvent(_Event):
    """Ошибка этапа. fatal=false: ход продолжается честным ответом без выдуманных данных."""

    type: Literal["error"] = "error"
    stage: Literal["stt", "router", "executor", "responder", "tts", "turn"]
    code: str = Field(examples=["stt_unavailable", "llm_unavailable", "router_invalid"])
    message: str
    fatal: bool = Field(description="true — ход прерван, событий этого хода больше не будет")


class TurnCancelledEvent(_Event):
    """Ход остановлен кнопкой «стоп». После него событий этого хода нет."""

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
    """Конец хода: трассировка в формате README кита и тайминги по этапам."""

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
    trace_id: str | None = Field(
        None, description="W3C trace-id хода (как `x-trace-id`): панель открывает "
        "`GET /traces/{trace_id}`. null — трассировка выключена"
    )


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
