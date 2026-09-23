"""Контракт ответа роутера — формат README кита (главнее спеки 4.1), `scenarios` первым полем."""

from typing import Any, Literal

from pydantic import BaseModel, Field

Language = Literal["ru", "kk", "mixed"]


class ScenarioPick(BaseModel):
    scenario_id: str
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class Alternative(BaseModel):
    scenario_id: str
    confidence: float = Field(ge=0, le=1)


class RouterOutput(BaseModel):
    scenarios: list[ScenarioPick] = Field(min_length=1)
    alternatives: list[Alternative] = []
    language: Language
    slots: dict[str, Any] = {}
    is_continuation: bool = False

    def unknown_ids(self, known: set[str]) -> list[str]:
        """ID, которых нет в каталоге. Непустой список — ошибка, а не «похожий» сценарий."""
        ids = [s.scenario_id for s in self.scenarios] + [a.scenario_id for a in self.alternatives]
        return [i for i in ids if i not in known]


class RouterResult(BaseModel):
    """Что вернул роутер: разобранный ответ + реальный промпт и сырой текст (спека 5.4)."""

    output: RouterOutput
    model: str
    prompt: str | None = None
    raw: str | None = None


class UnknownScenario(ValueError):
    def __init__(self, ids: list[str]):
        super().__init__(f"unknown scenario ids: {ids}")
        self.ids = ids
