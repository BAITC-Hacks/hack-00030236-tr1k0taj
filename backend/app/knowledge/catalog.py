"""Scenario / action / slot catalog. The router must always get ALL scenarios (no narrowing)."""

from app.knowledge.store import Store
from app.knowledge.types import Action, Scenario, Slot, SystemIntent


class Catalog:
    def __init__(self, store: Store):
        self._store = store

    async def scenarios(self) -> list[Scenario]:
        return [Scenario.model_validate(p) for p in await self._store.all("scenario")]

    async def scenario(self, scenario_id: str) -> Scenario | None:
        p = await self._store.get("scenario", scenario_id)
        return Scenario.model_validate(p) if p else None

    async def system_intents(self) -> list[SystemIntent]:
        return [SystemIntent.model_validate(p) for p in await self._store.all("system_intent")]

    async def scenario_ids(self) -> set[str]:
        """Every id the router may return: business scenarios + SYS_* intents."""
        return set(await self._store.keys("scenario")) | set(
            await self._store.keys("system_intent")
        )

    async def actions(self) -> list[Action]:
        return [Action.model_validate(p) for p in await self._store.all("action")]

    async def action(self, name: str) -> Action | None:
        p = await self._store.get("action", name)
        return Action.model_validate(p) if p else None

    async def slots(self) -> list[Slot]:
        return [Slot.model_validate(p) for p in await self._store.all("slot")]

    async def slot(self, name: str) -> Slot | None:
        p = await self._store.get("slot", name)
        return Slot.model_validate(p) if p else None

    async def queues(self) -> list[str]:
        return await self._store.keys("queue")

    async def validate_scenario(self, scenario: Scenario) -> list[str]:
        """Cross-reference checks for a new/edited scenario card. Returns human-readable errors."""
        errors = []
        known_actions = set(await self._store.keys("action"))
        known_slots = set(await self._store.keys("slot"))
        known_ids = await self.scenario_ids() | {scenario.scenario_id}
        errors += [f"unknown action: {a}" for a in scenario.actions if a not in known_actions]
        slots = scenario.slots.required + scenario.slots.optional
        errors += [f"unknown slot: {s}" for s in slots if s not in known_slots]
        errors += [
            f"not_this_if points to unknown scenario: {n.use_instead}"
            for n in scenario.not_this_if
            if n.use_instead not in known_ids
        ]
        if scenario.handoff and scenario.handoff.get("queue") not in set(await self.queues()):
            errors.append(f"unknown handoff queue: {scenario.handoff.get('queue')}")
        return errors
