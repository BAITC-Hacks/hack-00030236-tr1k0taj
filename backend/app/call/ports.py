"""Порты этапов хода. Модуль call зависит от них, а не от конкретных провайдеров (ADR 0008).

Владельцы портов: речь (STT/TTS, ProviderUnavailable) — app.speech, исполнитель/ответ/фон —
app.executor, роутер — здесь (ScenarioRouter). Реэкспорт ниже сохраняет старый путь импорта.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from app.executor import (
    ActionCall,
    ActionMode,
    Background,
    Execution,
    Executor,
    ReplyBrief,
    ReplyLanguage,
    Responder,
    TurnInput,
)
from app.knowledge import Knowledge
from app.router import RouterResult
from app.speech import AudioChunk, ProviderUnavailable, SpeechToText, TextToSpeech, Transcript

__all__ = [
    "ActionCall",
    "ActionMode",
    "AudioChunk",
    "Background",
    "Execution",
    "Executor",
    "ProviderUnavailable",
    "Providers",
    "ReplyBrief",
    "ReplyLanguage",
    "Responder",
    "ScenarioRouter",
    "SpeechToText",
    "TextToSpeech",
    "Transcript",
    "TurnInput",
]


class ScenarioRouter(Protocol):
    name: str

    async def route(self, utterance: str, view: dict[str, Any], kb: Knowledge) -> RouterResult: ...


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
