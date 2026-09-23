"""Порты речи. Боевые адаптеры STT/TTS реализуют их и регистрируются в app.speech.providers."""

from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import BaseModel

SpeechLanguage = Literal["ru", "kk"]


class Transcript(BaseModel):
    text: str
    language: str | None = None


class AudioChunk(BaseModel):
    mime: str
    data: bytes


class RealtimeSession(BaseModel):
    """Эфемерный секрет для realtime STT по WebRTC напрямую из браузера (ADR 0008)."""

    client_secret: str
    expires_at: int
    model: str


class SpeechToText(Protocol):
    name: str

    async def transcribe(
        self, audio: bytes, mime: str, language_hint: str | None
    ) -> Transcript: ...


class TextToSpeech(Protocol):
    name: str

    async def synthesize(self, text: str, language: SpeechLanguage) -> AudioChunk | None: ...

    def stream(self, text: str, language: SpeechLanguage) -> AsyncIterator[AudioChunk]:
        """Потоковый синтез: чанки PCM по мере готовности. Пусто — нечего озвучивать."""
        ...
