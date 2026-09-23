"""Моки речи для запуска без ключей (ADR 0008). Ведут себя честно: ничего не выдумывают."""

from app.speech.errors import ProviderUnavailable
from app.speech.ports import AudioChunk, SpeechLanguage, Transcript


class MockSpeechToText:
    name = "mock"

    async def transcribe(self, audio: bytes, mime: str, language_hint: str | None) -> Transcript:
        raise ProviderUnavailable(
            "stt",
            "stt_unavailable",
            "Распознавание речи не настроено (STT_PROVIDER=mock). Используйте текстовый ввод "
            "или задайте STT_PROVIDER и ключ в .env (см. README).",
        )


class MockTextToSpeech:
    name = "mock"

    async def synthesize(self, text: str, language: SpeechLanguage) -> AudioChunk | None:
        return None
