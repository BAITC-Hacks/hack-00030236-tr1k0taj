"""Выбор реализаций портов через env (ADR 0008). Адаптеры речи регистрируются в app.speech,
роутер — здесь по имени провайдера."""

from app.call.mocks import MockRouter
from app.call.ports import Providers
from app.config import Settings
from app.executor import BaselineExecutor, NoopBackground, TemplateResponder
from app.knowledge import Knowledge
from app.router import OpenAIRouter, RouterResult, RouterUnavailable
from app.speech import STT, TTS, ProviderUnavailable, build_stt, build_tts


class _RouterErrors:
    """Переводит ошибку роутера в ProviderUnavailable: ход не падает, SSE отдаёт error с кодом."""

    def __init__(self, inner):
        self._inner = inner
        self.name = inner.name

    async def route(self, utterance: str, view: dict, kb: Knowledge) -> RouterResult:
        try:
            return await self._inner.route(utterance, view, kb)
        except RouterUnavailable as e:
            raise ProviderUnavailable("router", e.code, e.message) from e


LLM = {"mock": MockRouter, "openai": lambda: _RouterErrors(OpenAIRouter())}

__all__ = ["LLM", "STT", "TTS", "build_providers"]


def _pick(registry: dict, name: str, env: str):
    try:
        return registry[name]()
    except KeyError:
        raise RuntimeError(
            f"{env}={name!r} не поддерживается. Доступно: {', '.join(registry)}"
        ) from None


def build_providers(settings: Settings) -> Providers:
    return Providers(
        stt=build_stt(settings.stt_provider),
        router=_pick(LLM, settings.llm_provider, "LLM_PROVIDER"),
        executor=BaselineExecutor(),
        responder=TemplateResponder(),
        tts=build_tts(settings.tts_provider),
        background=NoopBackground(),
    )
