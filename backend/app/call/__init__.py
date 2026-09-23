"""Модуль call: HTTP звонка и оркестрация хода с потоковым ответом (docs/specs/call-api.md).

Порты этапов: STT/TTS — app.speech, исполнитель/ответ/фон — app.executor, роутер —
app.call.ports (там же реэкспорт всех портов); реализации выбираются через env в build_providers.
"""

from app.call.api import router as api_router
from app.call.events import TurnEvent
from app.call.ports import (
    ActionCall,
    AudioChunk,
    Background,
    Execution,
    Executor,
    Providers,
    ProviderUnavailable,
    ReplyBrief,
    Responder,
    ScenarioRouter,
    SpeechToText,
    TextToSpeech,
    Transcript,
    TurnInput,
)
from app.call.providers import build_providers
from app.call.service import CallService, warm_tts_filler

__all__ = [
    "ActionCall",
    "AudioChunk",
    "Background",
    "CallService",
    "Execution",
    "Executor",
    "ProviderUnavailable",
    "Providers",
    "ReplyBrief",
    "Responder",
    "ScenarioRouter",
    "SpeechToText",
    "TextToSpeech",
    "Transcript",
    "TurnEvent",
    "TurnInput",
    "api_router",
    "build_providers",
    "warm_tts_filler",
]
