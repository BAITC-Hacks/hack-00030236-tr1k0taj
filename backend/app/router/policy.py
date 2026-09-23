"""Политика порогов (README кита, спека 4.2). Чистая функция, без LLM."""

from typing import Literal

from pydantic import BaseModel

from app.router.types import RouterOutput, ScenarioPick

ROUTE = 0.75
CLARIFY = 0.45

DecisionKind = Literal["route", "clarify", "handoff"]


class Decision(BaseModel):
    kind: DecisionKind
    scenarios: list[ScenarioPick]  # порядок обслуживания: urgent первыми
    clarify_options: list[str] = []  # два самых вероятных ID для переспроса


def decide(out: RouterOutput, priorities: dict[str, str], low_streak: int) -> Decision:
    """low_streak — сколько ходов подряд до этого уверенность была < 0.45."""
    top = out.scenarios[0].confidence
    if top >= ROUTE:
        picked = [s for s in out.scenarios if s.confidence >= ROUTE]
        urgent = [s for s in picked if priorities.get(s.scenario_id) == "urgent"]
        rest = [s for s in picked if priorities.get(s.scenario_id) != "urgent"]
        return Decision(kind="route", scenarios=urgent + rest)
    if top < CLARIFY and low_streak >= 1:
        return Decision(kind="handoff", scenarios=out.scenarios)
    candidates = sorted(
        [(s.scenario_id, s.confidence) for s in out.scenarios]
        + [(a.scenario_id, a.confidence) for a in out.alternatives],
        key=lambda x: -x[1],
    )
    options = list(dict.fromkeys(i for i, _ in candidates if not i.startswith("SYS_")))[:2]
    return Decision(kind="clarify", scenarios=out.scenarios, clarify_options=options)
