"""Модуль speech: порты и адаптеры STT/TTS, разбиение ответа на предложения (docs/specs/speech-module.md).

Слой предобработки речи: не зависит от агентного слоя (kernel, router, executor), context и knowledge.
"""

from app.speech.errors import ProviderUnavailable
from app.speech.mocks import MockSpeechToText, MockTextToSpeech
from app.speech.ports import (
    AudioChunk,
    RealtimeSession,
    SpeechLanguage,
    SpeechToText,
    TextToSpeech,
    Transcript,
)
from app.speech.providers import STT, TTS, build_stt, build_tts, create_realtime_stt_session
from app.speech.text import SENTENCE_END, split_sentences

__all__ = [
    "SENTENCE_END",
    "STT",
    "TTS",
    "AudioChunk",
    "MockSpeechToText",
    "MockTextToSpeech",
    "ProviderUnavailable",
    "RealtimeSession",
    "SpeechLanguage",
    "SpeechToText",
    "TextToSpeech",
    "Transcript",
    "build_stt",
    "build_tts",
    "create_realtime_stt_session",
    "split_sentences",
]
