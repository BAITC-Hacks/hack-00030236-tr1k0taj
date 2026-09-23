"""Порты этапов хода. Модуль call зависит от них, а не от конкретных провайдеров (ADR 0008).

Боевые реализации: STT/TTS — app.speech (зона C), роутер — app.router, исполнитель — app.executor,
фоновый помощник — app.background. Моки — app.call.mocks.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from app.context import Contexts, Fact, SessionContext
from app.knowledge import Knowledge
from app.router import Decision, RouterOutput, RouterResult

ReplyLanguage = Literal["ru", "kk"]
ActionMode = Literal["read", "preview", "execute", "handoff", "unsupported"]


class ProviderUnavailable(Exception):
    """Провайдер не настроен или упал. Ход не роняем: отдаём событие error с этим кодом."""

    def __init__(self, stage: str, code: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message


class Transcript(BaseModel):
    text: str
    language: str | None = None


class AudioChunk(BaseModel):
    mime: str
    data: bytes


class ActionCall(BaseModel):
    """Вызов действия из actions.json так, как его показывает панель и README кита."""

    name: str
    mode: ActionMode
    params: dict[str, Any] = {}
    ok: bool = True
    result: Any = None
    error: dict[str, str] | None = None  # {"code": "not_found", "message": "..."}


class ReplyBrief(BaseModel):
    """Всё, что нужно генератору ответа. Факты — только из данных, с источником."""

    language: ReplyLanguage
    scenario_id: str | None = None
    decision: str | None = None
    instruction: str  # что сказать по смыслу, для LLM
    template: str | None = None  # ориентир из responses кита / системных намерений
    facts: list[Fact] = []
    handoff: dict[str, Any] | None = None


class Execution(BaseModel):
    actions: list[ActionCall] = []
    facts: list[Fact] = []
    brief: ReplyBrief


@dataclass
class TurnInput:
    """Вход исполнителя: решение роутера + доступ к контексту и данным."""

    session_id: str
    turn_id: int
    transcript: str
    router: RouterOutput
    decision: Decision
    snapshot: SessionContext  # снимок до изменений этого хода
    contexts: Contexts  # единственный писатель состояния
    kb: Knowledge


class SpeechToText(Protocol):
    name: str

    async def transcribe(
        self, audio: bytes, mime: str, language_hint: str | None
    ) -> Transcript: ...


class ScenarioRouter(Protocol):
    name: str

    async def route(self, utterance: str, view: dict[str, Any], kb: Knowledge) -> RouterResult: ...


class Executor(Protocol):
    name: str
    supported_actions: list[str]

    async def execute(self, turn: TurnInput) -> Execution: ...


class Responder(Protocol):
    name: str

    def stream(self, brief: ReplyBrief) -> AsyncIterator[str]: ...


class TextToSpeech(Protocol):
    name: str

    async def synthesize(self, text: str, language: ReplyLanguage) -> AudioChunk | None: ...


class Background(Protocol):
    name: str

    def schedule(self, snapshot: SessionContext, turn_id: int) -> None: ...


@dataclass
class Providers:
    stt: SpeechToText
    router: ScenarioRouter
    executor: Executor
    responder: Responder
    tts: TextToSpeech
    background: Background

    @property
    def mock_mode(self) -> bool:
        return self.router.name == "mock"
