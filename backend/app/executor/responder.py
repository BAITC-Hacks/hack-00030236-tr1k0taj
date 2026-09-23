"""Генератор ответа без LLM (ADR 0008)."""

from collections.abc import AsyncIterator

from app.executor.ports import ReplyBrief


class TemplateResponder:
    """Отдаёт ориентир кита кусками, как отдавал бы LLM. Без LLM."""

    name = "template"

    async def stream(self, brief: ReplyBrief) -> AsyncIterator[str]:
        words = (brief.template or brief.instruction).split(" ")
        for i, w in enumerate(words):
            yield w if i == 0 else " " + w
