"""Выбор адаптеров речи через env (ADR 0008). Боевые адаптеры регистрируются здесь по имени."""

from app.speech.errors import ProviderUnavailable
from app.speech.mocks import MockSpeechToText, MockTextToSpeech
from app.speech.openai_speech import OpenAISpeechToText, OpenAITextToSpeech, create_realtime_session
from app.speech.ports import RealtimeSession, SpeechToText, TextToSpeech

STT = {"mock": MockSpeechToText, "openai": OpenAISpeechToText}
TTS = {"mock": MockTextToSpeech, "openai": OpenAITextToSpeech}


def _pick(registry: dict, name: str, env: str):
    try:
        return registry[name]()
    except KeyError:
        raise RuntimeError(
            f"{env}={name!r} не поддерживается. Доступно: {', '.join(registry)}"
        ) from None


def build_stt(name: str) -> SpeechToText:
    return _pick(STT, name, "STT_PROVIDER")


def build_tts(name: str) -> TextToSpeech:
    return _pick(TTS, name, "TTS_PROVIDER")


async def create_realtime_stt_session(stt_provider: str, language_hint: str | None) -> RealtimeSession:
    """Эфемерный секрет для STT по WebRTC (call/api.py `/stt/session`); mock — понятная 409."""
    if stt_provider != "openai":
        raise ProviderUnavailable(
            "stt", "stt_unavailable",
            "Realtime-распознавание недоступно (STT_PROVIDER не openai). Используйте запись хода.",
        )
    return await create_realtime_session(language_hint)
