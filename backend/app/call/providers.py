"""Выбор реализаций портов через env (ADR 0008). Сейчас есть только моки; боевые адаптеры
регистрируются здесь по имени провайдера."""

from app.call.mocks import (
    BaselineExecutor,
    MockRouter,
    MockSpeechToText,
    MockTextToSpeech,
    NoopBackground,
    TemplateResponder,
)
from app.call.ports import Providers
from app.config import Settings

STT = {"mock": MockSpeechToText}
LLM = {"mock": MockRouter}
TTS = {"mock": MockTextToSpeech}


def _pick(registry: dict, name: str, env: str):
    try:
        return registry[name]()
    except KeyError:
        raise RuntimeError(
            f"{env}={name!r} не поддерживается. Доступно: {', '.join(registry)}"
        ) from None


def build_providers(settings: Settings) -> Providers:
    return Providers(
        stt=_pick(STT, settings.stt_provider, "STT_PROVIDER"),
        router=_pick(LLM, settings.llm_provider, "LLM_PROVIDER"),
        executor=BaselineExecutor(),
        responder=TemplateResponder(),
        tts=_pick(TTS, settings.tts_provider, "TTS_PROVIDER"),
        background=NoopBackground(),
    )
