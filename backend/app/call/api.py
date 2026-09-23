"""HTTP звонка: сессия, ход с потоковым ответом (SSE), стоп, замер воспроизведения.

Спека: docs/specs/call-api.md. Контекст и доска для панели — /sessions/* (модуль context).
"""

import json
from contextlib import aclosing
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.call.ports import Providers
from app.call.service import CallService
from app.config import DATASET_TODAY
from app.context import SessionContext, SessionNotFound
from app.docs import SSE_EXAMPLE
from app.kernel import KernelError, TurnRequest
from app.router import RouterResult

MAX_AUDIO_BYTES = 10 * 1024 * 1024

router = APIRouter(tags=["call"])


def get_calls(request: Request) -> CallService:
    return request.app.state.calls


Calls = Annotated[CallService, Depends(get_calls)]
SessionId = Annotated[
    UUID,
    Path(
        description="UUID сессии — ключ потока общения. Генерирует фронт (`crypto.randomUUID()`), "
        "его же возвращает POST /calls, если UUID не передан."
    ),
]
TurnId = Annotated[int, Path(description="turn_id из события turn.started")]


async def existing_session(session_id: SessionId, calls: Calls) -> str:
    sid = str(session_id)
    try:
        await calls.contexts.snapshot(sid)
    except SessionNotFound:
        raise HTTPException(404, "session not found") from None
    return sid


Session = Annotated[str, Depends(existing_session)]


def _not_busy(sid: str, calls: CallService) -> str:
    """Один foreground-ход на сессию (спека 7.5): пока идёт ход, новый ввод — 409."""
    if calls.busy(sid):
        raise HTTPException(409, "turn_in_progress")
    return sid


async def turn_session(session_id: SessionId, calls: Calls) -> str:
    """Ход по UUID с фронта: незнакомая сессия открывается сама (первый ход или рестарт backend)."""
    sid = str(session_id)
    await calls.ensure_call(sid)
    return sid


TurnSession = Annotated[str, Depends(turn_session)]

NOT_FOUND = {404: {"description": "Сессии нет (или backend перезапускался)"}}
TURN_ERRORS = {
    200: {
        "description": "Поток событий хода. Пример ниже показывает форму, числа в нём условные.",
        "content": {"text/event-stream": {"example": SSE_EXAMPLE}},
    },
    409: {"description": "`turn_in_progress`: предыдущий ход ещё идёт"},
    422: {"description": "Пустая реплика или аудио, `session_id` не UUID"},
}
SSE_DOC = """Ответ — поток `text/event-stream`. Каждое событие: `event: <type>` и `data: <JSON>`,
в JSON есть `type` и `turn_id`. Типичный порядок:

`transcript` → `turn.started` → `routing` → `action`* → `facts`? → `reply.delta`+ →
`audio`* (вперемешку с `reply.delta`) → `reply.done` → `turn.done`.

Ошибка этапа приходит как `error` (`fatal=false` — ход продолжается честным ответом).
После «стопа» приходит `turn.cancelled` (если соединение ещё открыто) и больше ничего.
Если событий долго нет, сервер шлёт комментарий-keepalive `: ping`."""


# --- схемы ------------------------------------------------------------------


class Capabilities(BaseModel):
    mock_mode: bool = Field(description="true — LLM не настроен, сценарии не выбираются")
    providers: dict[str, str] = Field(examples=[{"stt": "mock", "llm": "mock", "tts": "mock"}])
    supported_actions: list[str] = Field(
        description="Действия, которые исполнитель реально выполняет; остальные ведут к оператору"
    )
    languages: list[str] = ["ru", "kk", "mixed"]
    audio_input: list[str] = ["audio/webm", "audio/ogg", "audio/wav"]
    dataset_today: str
    kernel_enabled: bool = False
    kernel_streaming: bool = False
    kernel_background: bool = False
    kernel_playback: str | None = None


class StartCall(BaseModel):
    session_id: UUID | None = Field(
        None,
        description="UUID сессии с фронта. Не передан — сервер сгенерирует свой.",
        examples=["3f1c2a9e-8b7d-4c61-9f0e-2d5a7b6c4e10"],
    )


class CallStarted(BaseModel):
    session_id: str = Field(description="UUID сессии, по нему идут все остальные вызовы")
    created: bool = Field(description="false — сессия с этим UUID уже была, вернули её состояние")
    context: SessionContext
    capabilities: Capabilities


class TextTurn(TurnRequest):
    request_id: UUID | None = None
    text: str = Field(
        min_length=1, max_length=2000, examples=["Что с моим заявлением по затоплению?"]
    )
    language_hint: Literal["ru", "kk"] | None = Field(
        None, description="Подсказка языка; роутер всё равно определяет язык сам"
    )


class AudioTurn(BaseModel):
    request_id: UUID | None = None
    audio: UploadFile = Field(description="Запись push-to-talk: webm/ogg/wav, до 10 МБ")
    language_hint: Literal["ru", "kk"] | None = Field(
        None, description="Подсказка языка; роутер всё равно определяет язык сам"
    )


class PlaybackSegment(BaseModel):
    segment_id: UUID
    start_ms: int = Field(ge=0, le=3600000)
    end_ms: int = Field(gt=0, le=3600000)

    @model_validator(mode="after")
    def positive_interval(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class CancelTurn(BaseModel):
    played_ms: int | None = Field(None, ge=0, le=3600000)


class Playback(BaseModel):
    eos_to_playback_ms: int | None = Field(
        None, ge=0, description="Браузер: конец речи клиента → начало воспроизведения ответа"
    )
    eos_to_reply_text_ms: int | None = Field(
        None, ge=0, description="Браузер: конец речи → появление текста ответа"
    )
    request_id: UUID | None = None
    response_id: UUID | None = None
    played_ms: int | None = Field(None, ge=0, le=3600000)
    segments: list[PlaybackSegment] = Field(default_factory=list, max_length=16)
    text_segment_ids: list[UUID] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def has_measurement(self):
        if self.played_ms is None and any((self.response_id, self.segments, self.text_segment_ids)):
            raise ValueError("played_ms is required for response delivery feedback")
        if self.eos_to_playback_ms is None and self.eos_to_reply_text_ms is None \
                and self.played_ms is None:
            raise ValueError("playback requires a timing measurement or played_ms")
        return self


class RouterDebug(BaseModel):
    """Устройство слоя (спека 5.4): что реально ушло в модель и что вернулось."""

    turn_id: int | None
    result: RouterResult | None


def capabilities(p: Providers, kernel=None) -> Capabilities:
    return Capabilities(
        mock_mode=p.mock_mode,
        providers={
            "stt": p.stt.name,
            "llm": p.router.name,
            "executor": p.executor.name,
            "responder": "kernel" if kernel is not None else p.responder.name,
            "tts": p.tts.name,
            "background": "kernel" if kernel is not None else p.background.name,
        },
        supported_actions=p.executor.supported_actions,
        dataset_today=DATASET_TODAY.isoformat(),
        kernel_enabled=kernel is not None,
        kernel_streaming=kernel is not None,
        kernel_background=kernel is not None,
        kernel_playback="segment-timeline-v1" if kernel is not None else None,
    )


# --- эндпоинты ---------------------------------------------------------------


@router.get("/capabilities", summary="Что умеет backend сейчас")
async def get_capabilities(calls: Calls) -> Capabilities:
    return capabilities(calls.providers, calls.kernel)


@router.post(
    "/calls",
    status_code=201,
    summary="Начать звонок",
    description="Идемпотентно по `session_id`: 201 — новая сессия, 200 — сессия уже была "
    "(состояние не сбрасывается; для нового звонка есть /reset). Вызов необязателен: "
    "первый ход по новому UUID открывает звонок сам.",
    responses={200: {"description": "Сессия с этим UUID уже существует", "model": CallStarted}},
)
async def start_call(
    calls: Calls, response: Response, body: StartCall | None = None
) -> CallStarted:
    sid = str(body.session_id) if body and body.session_id else str(uuid4())
    ctx, created = await calls.ensure_call(sid)
    if not created:
        response.status_code = 200
    return CallStarted(
        session_id=sid, created=created, context=ctx,
        capabilities=capabilities(calls.providers, calls.kernel),
    )


@router.post(
    "/calls/{session_id}/reset",
    summary="Новый звонок в той же сессии",
    description="`generation + 1`, контекст и темы очищаются, история прошлого звонка не протекает.",
    responses={409: TURN_ERRORS[409]},
)
async def reset_call(session_id: SessionId, calls: Calls) -> SessionContext:
    return await calls.contexts.start_call(_not_busy(str(session_id), calls))


@router.post(
    "/calls/{session_id}/turns/text",
    response_class=StreamingResponse,
    summary="Реплика текстом → поток событий",
    description="Резервный канал ввода (спека 2.1). " + SSE_DOC,
    responses=TURN_ERRORS,
)
async def text_turn(
    session_id: TurnSession, body: TextTurn, calls: Calls
) -> StreamingResponse:
    request_id, is_new = await calls.ingress(
        session_id, text=body.text, language_hint=body.language_hint,
        request_id=body.request_id, task_id=body.task_id, updates=body.updates,
        interrupt_previous=body.interrupt_previous,
    )
    return _stream_response(calls, session_id, request_id, cancel_on_disconnect=is_new)


@router.post(
    "/calls/{session_id}/turns/audio",
    response_class=StreamingResponse,
    summary="Реплика голосом → поток событий",
    description="multipart/form-data: `audio` (webm/ogg/wav, до 10 МБ) и `language_hint`. "
    + SSE_DOC,
    responses=TURN_ERRORS | {413: {"description": "Аудио больше 10 МБ"}},
)
async def audio_turn(
    session_id: TurnSession,
    calls: Calls,
    form: Annotated[AudioTurn, Form(media_type="multipart/form-data")],
) -> StreamingResponse:
    audio = form.audio
    data = await audio.read()
    if not data:
        raise HTTPException(422, "empty audio")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too large")
    request_id, is_new = await calls.ingress(
        session_id,
        audio=data,
        mime=audio.content_type or "audio/webm",
        language_hint=form.language_hint,
        request_id=form.request_id,
    )
    return _stream_response(calls, session_id, request_id, cancel_on_disconnect=is_new)


def _stream_response(calls, sid, request_id=None, *, after=0, cancel_on_disconnect=False):
    async def stream():
        async with aclosing(calls.replay(
            sid, after=after, request_id=request_id, cancel_on_disconnect=cancel_on_disconnect,
        )) as events:
            async for envelope in events:
                if envelope is None:
                    yield ": keep-alive\n\n"
                else:
                    yield (f"id: {envelope['event_seq']}\nevent: {envelope['type']}\n"
                           f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n")

    headers = {"X-Request-ID": request_id} if request_id is not None else {}
    headers.update({"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@router.get("/calls/{session_id}/events", response_class=StreamingResponse,
            summary="Продолжить чтение сохранённого публичного потока звонка")
async def replay_events(
    session_id: Session, calls: Calls,
    after: Annotated[int, Query(ge=0)] = 0,
    request_id: UUID | None = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    if last_event_id is not None:
        try:
            header_cursor = int(last_event_id)
        except ValueError as exc:
            raise KernelError("invalid_cursor", "Last-Event-ID должен быть числом", 422) from exc
        if header_cursor < 0:
            raise KernelError("invalid_cursor", "Курсор не может быть отрицательным", 422)
        after = max(after, header_cursor)
    rid = str(request_id) if request_id is not None else None
    await calls.prepare_replay(session_id, after, rid)
    return _stream_response(calls, session_id, rid, after=after)


@router.post(
    "/calls/{session_id}/turns/{turn_id}/cancel",
    status_code=204,
    summary="Стоп: отменить ход",
    description="Кнопка «стоп» (спека 7.5): поздние текст и аудио этого хода не придут. "
    "Обрыв fetch на клиенте тоже отменяет ход.",
    responses=NOT_FOUND,
)
async def cancel_turn(
    session_id: Session, turn_id: TurnId, calls: Calls, body: CancelTurn | None = None
) -> None:
    await calls.cancel_turn(session_id, turn_id, body.played_ms if body else None)


@router.post(
    "/calls/{session_id}/turns/{turn_id}/playback",
    status_code=204,
    summary="Замер из браузера",
    description="Фронт присылает время «конец речи → начало звука» (спека 10.2). "
    "Пишется на доску как timing, версию контекста не меняет.",
    responses=NOT_FOUND,
)
async def report_playback(
    session_id: Session, turn_id: TurnId, body: Playback, calls: Calls
) -> None:
    if body.played_ms is not None:
        if calls.kernel is None:
            raise HTTPException(409, "kernel_playback_unavailable")
        await calls.kernel.playback_turn(
            session_id, turn_id, body.model_dump(mode="json", exclude_none=True)
        )
    timing = body.model_dump(include={"eos_to_playback_ms", "eos_to_reply_text_ms"},
                             exclude_none=True)
    if timing:
        await calls.contexts.log(
            session_id, turn_id, "timing", "user", {"stage": "browser", **timing}
        )


@router.get(
    "/calls/{session_id}/router/last",
    summary="Последний промпт и ответ роутера",
    description="Экран «устройство слоя» (спека 5.4). `result=null`, пока роутер не отвечал.",
    responses=NOT_FOUND,
)
async def last_router(session_id: Session, calls: Calls) -> RouterDebug:
    snap = await calls.contexts.snapshot(session_id)
    rr = calls.last_router.get(session_id)
    return RouterDebug(turn_id=snap.turn_id if rr else None, result=rr)
