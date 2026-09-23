"""Модуль router: контракт ответа LLM-роутера и политика порогов (ADR 0004).

Боевой LLM-роутер реализует порт `app.call.ScenarioRouter` и живёт здесь же.
"""

from app.router.openai_router import OpenAIRouter, RouterUnavailable, warmup
from app.router.policy import CLARIFY, ROUTE, Decision, decide
from app.router.types import (
    Alternative,
    Language,
    RouterOutput,
    RouterResult,
    ScenarioPick,
    UnknownScenario,
)

__all__ = [
    "CLARIFY",
    "ROUTE",
    "Alternative",
    "Decision",
    "Language",
    "OpenAIRouter",
    "RouterOutput",
    "RouterResult",
    "RouterUnavailable",
    "ScenarioPick",
    "UnknownScenario",
    "decide",
    "warmup",
]
