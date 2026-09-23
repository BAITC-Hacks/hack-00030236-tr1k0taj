"""Typed views over starter-kit records. Kit fields are kept verbatim (extra="allow")."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class KitModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class NotThisIf(KitModel):
    condition: str
    use_instead: str


class ScenarioSlots(KitModel):
    required: list[str] = []
    optional: list[str] = []


class Scenario(KitModel):
    scenario_id: str
    slug: str
    name: str
    description: str
    domain: str
    category: str
    priority: Literal["normal", "high", "urgent"]
    not_this_if: list[NotThisIf] = []
    requires_identification: bool = False
    requires_confirmation: bool = False
    slots: ScenarioSlots = ScenarioSlots()
    actions: list[str] = []
    handoff: dict[str, Any] | None = None
    examples: dict[str, list[str]] = {}
    responses: dict[str, Any] = {}


class SystemIntent(KitModel):
    id: str
    description: str
    behavior: str
    response: dict[str, str] = {}


class Action(KitModel):
    name: str
    description: str
    inputs: list[str] = []
    outputs: list[str] = []
    errors: list[str] = []
    irreversible: bool


class Slot(KitModel):
    name: str
    type: str
    description: str
    prompt: dict[str, str] = {}
    pattern: str | None = None
    values: list[Any] | None = None


class Client(KitModel):
    client_id: str
    full_name: str
    phone: str
    iin: str
    city: str
    email: str
    address: str
    preferred_language: str


class Policy(KitModel):
    policy_number: str
    client_id: str
    product: str
    start_date: str
    end_date: str


class Claim(KitModel):
    claim_number: str
    client_id: str
    policy_number: str
    status: str
    next_step: str


class Payment(KitModel):
    payment_id: str
    client_id: str
    date: str
    amount: int
    status: str


class Fact(BaseModel):
    """A fact for the blackboard (ADR 0003): every value carries its source."""

    key: str
    value: Any
    source: str
    source_id: str


class Hit(BaseModel):
    kind: str
    key: str
    score: float
    snippet: str
    payload: Any


# Payload models per kind; used to validate CRUD writes. Other kinds are free-form JSON.
KIND_MODELS: dict[str, type[KitModel]] = {
    "scenario": Scenario,
    "system_intent": SystemIntent,
    "action": Action,
    "slot": Slot,
    "client": Client,
    "policy": Policy,
    "claim": Claim,
    "payment": Payment,
}

# Field that holds the record key inside the payload, per kind.
KEY_FIELDS: dict[str, str] = {
    "scenario": "scenario_id",
    "system_intent": "id",
    "action": "name",
    "slot": "name",
    "client": "client_id",
    "policy": "policy_number",
    "claim": "claim_number",
    "payment": "payment_id",
}
