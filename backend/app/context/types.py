"""Публичные типы модуля context (ADR 0003, спека 4.3–4.5)."""

from typing import Any, Literal

from pydantic import BaseModel, Field

Author = Literal["stt", "router", "executor", "background", "scheduler", "user", "system"]
EntryType = Literal[
    "call",  # старт звонка / сброс поколения
    "utterance",  # реплика клиента
    "response",  # реплика бота
    "routing",  # решение роутера
    "client",  # идентификация / смена клиента
    "topic_stack",  # активная тема и стек отложенных
    "slots",
    "facts",
    "confirmation",  # запрос / отмена / исполнение подтверждения
    "patch",  # результат context_patch фонового помощника
    "action",  # вызов действия
    "timing",
    "turn",  # отмена хода кнопкой «стоп»
    "trace",
    "error",
    "kernel",  # runtime envelopes; internal ones are excluded from the public board
]
Origin = Literal["foreground", "background"]


class Fact(BaseModel):
    """Факт из данных. Без source факта нет (спека 3.3)."""

    key: str
    value: Any
    source: str = Field(min_length=1)
    source_id: str | None = None
    origin: Origin = "foreground"
    context_version: int = 0  # проставляет модуль при записи


class PendingConfirmation(BaseModel):
    """Подтверждение привязано к действию и параметрам (ADR 0005)."""

    action: str
    params: dict[str, Any]
    topic: str | None = None
    turn_id: int


class Turn(BaseModel):
    turn_id: int
    role: Literal["client", "bot"]
    text: str
    language: str | None = None
    task_id: str = "default"


class SessionContext(BaseModel):
    """Состояние звонка. Наружу отдаётся только копией — правка копии ничего не меняет."""

    session_id: str
    generation: int = 1
    context_version: int = 0
    turn_id: int = 0
    client_id: str | None = None
    language: str | None = None
    active_scenario: str | None = None
    pending_topics: list[str] = []  # стек, вершина — последний элемент
    slots_by_topic: dict[str, dict[str, Any]] = {}
    pending_confirmation: PendingConfirmation | None = None
    facts: list[Fact] = []
    history: list[Turn] = []
    low_confidence_streak: int = 0
    cancelled_turns: list[int] = []
    domain_task_id: str = "default"
    task_states: dict[str, dict[str, Any]] = Field(default_factory=dict, exclude=True, repr=False)
    # Runtime projection is persisted by ContextStore, never serialized into public /context.
    kernel: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)
    call_journal: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)


class BoardEntry(BaseModel):
    session_id: str
    generation: int
    turn_id: int
    type: EntryType
    author: Author
    payload: dict[str, Any] = {}
    source: str | None = None
    source_id: str | None = None
    confidence: float | None = None
    ts_start_ms: int
    ts_end_ms: int
    context_version: int


class ContextPatch(BaseModel):
    """Патч фонового помощника (спека 4.5)."""

    session_id: str
    generation: int
    based_on_turn_id: int
    base_context_version: int
    client_id: str | None
    facts: list[Fact]


class PatchResult(BaseModel):
    status: Literal["applied", "stale", "rejected"]
    reason: str | None = None
    context_version: int


class SessionNotFound(KeyError):
    pass
