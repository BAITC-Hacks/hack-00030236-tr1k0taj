"""Выбор адаптеров речи через env (ADR 0008). Боевые адаптеры регистрируются здесь по имени."""

from app.speech.mocks import MockSpeechToText, MockTextToSpeech
from app.speech.openai_speech import OpenAISpeechToText, OpenAITextToSpeech
from app.speech.ports import SpeechToText, TextToSpeech

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
