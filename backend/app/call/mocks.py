"""Моки портов для запуска без ключей (ADR 0008). Ведут себя честно: ничего не выдумывают.

Мок роутера живёт здесь; моки речи — в app.speech, исполнитель/ответ/фон — в app.executor
(реэкспорт ниже сохраняет старый путь импорта).
"""

from app.executor import BaselineExecutor, NoopBackground, TemplateResponder, reply_language
from app.knowledge import Knowledge
from app.router import RouterResult
from app.speech import MockSpeechToText, MockTextToSpeech, ProviderUnavailable

__all__ = [
    "BaselineExecutor",
    "MockRouter",
    "MockSpeechToText",
    "MockTextToSpeech",
    "NoopBackground",
    "TemplateResponder",
    "reply_language",
]


class MockRouter:
    name = "mock"

    async def route(self, utterance: str, view: dict, kb: Knowledge) -> RouterResult:
        raise ProviderUnavailable(
            "router",
            "llm_unavailable",
            "LLM-роутер не настроен (LLM_PROVIDER=mock): задайте провайдера и ключ в .env "
            "(см. README). Сценарий не выбран.",
        )
