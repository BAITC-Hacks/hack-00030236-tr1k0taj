"""Модуль router: контракт ответа LLM-роутера и политика порогов (ADR 0004).

Боевой LLM-роутер реализует порт `app.call.ScenarioRouter` и живёт здесь же.
"""

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
    "RouterOutput",
    "RouterResult",
    "ScenarioPick",
    "UnknownScenario",
    "decide",
]
