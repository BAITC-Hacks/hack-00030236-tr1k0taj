"""Боевой LLM-роутер на OpenAI Responses API (ADR 0004, спека 5).

Промпт: неизменная системная часть (правила + карточки ВСЕХ 40 сценариев + SYS_* + слоты) идёт
первой, чтобы работал prompt caching OpenAI; переменная часть — JSON с репликой и router_view.
Ответ — strict json_schema, ID сценариев ограничены enum каталога.
"""

import hashlib
import json
from typing import Any

from app.config import DATASET_TODAY, settings
from app.knowledge import Knowledge, Scenario, Slot, SystemIntent
from app.router.types import RouterOutput, RouterResult


class RouterUnavailable(Exception):
    """Роутер не настроен или провайдер упал. call превращает это в событие error с кодом."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


RULES = f"""You are the scenario router of the Saqta Insurance voice contact center.
Clients speak Russian, Kazakh or a mix. Today is {DATASET_TODAY.isoformat()} (dataset date).
Your only job: pick which catalog scenario(s) the client's CURRENT utterance asks for.
You never answer the client and never invent facts.

Rules:
- Choose ONLY from the catalog below (SC01..SC40 and SYS_*). Read every card; respect not_this_if
  boundaries: if a not_this_if condition matches, use the scenario named in use_instead.
- scenarios: sorted by confidence, first = main intent. If the client asks for several different
  things in one utterance, return every one of them (multi-intent), in spoken order.
- SYS_OUT_OF_SCOPE: not about Saqta insurance services. SYS_GOODBYE: client ends the call.
  SYS_UNCLEAR: intent cannot be determined; still list the 2 most likely scenarios in alternatives.
- confidence 0..1: >=0.75 only when the utterance clearly matches; 0.45-0.75 when two scenarios
  are plausible; lower when unsure.
- reason: short verifiable features of the utterance and the boundary vs the neighbour scenario.
- alternatives: up to 3 other plausible scenario ids with confidence (may be empty).
- language: ru | kk | mixed (mixed = both languages in the utterance).
- slots: only values the client explicitly said in this utterance, normalized to the slot
  format below (phones +7XXXXXXXXXX, dates YYYY-MM-DD resolved against today, enum values
  exactly as listed). Never guess slot values. Empty list if none.
- is_continuation: true if the utterance continues the active scenario from context
  (answers its question, gives a slot, confirms/declines), false for a new topic. When it is a
  continuation, return the active scenario as the main one.
- search_query: short English retrieval query for the knowledge base, or null.
Output strictly the JSON schema, no text outside fields.
"""


def _card(s: Scenario) -> str:
    lines = [
        f"## {s.scenario_id} | {s.name} | priority: {s.priority}",
        f"description: {s.description}",
    ]
    for n in s.not_this_if:
        lines.append(f"not_this_if: {n.condition} -> {n.use_instead}")
    flags = [f for f, on in (("requires_identification", s.requires_identification),
                             ("requires_confirmation", s.requires_confirmation)) if on]
    slots = f"slots required: {', '.join(s.slots.required) or '-'}; optional: " \
            f"{', '.join(s.slots.optional) or '-'}"
    lines.append(slots + (f"; {', '.join(flags)}" if flags else ""))
    for lang in ("ru", "kk"):  # спека 5.2: примеры ru[2], kk[2] из scenarios.json
        if ex := s.examples.get(lang, [])[:2]:
            lines.append(f"examples {lang}: " + " | ".join(ex))
    return "\n".join(lines)


def _slot(s: Slot) -> str:
    fmt = f"pattern {s.pattern}" if s.pattern else (
        f"values {', '.join(map(str, s.values))}" if s.values else "")
    return f"- {s.name} ({s.type}{'; ' + fmt if fmt else ''}): {s.description}"


def render_system_prompt(
    scenarios: list[Scenario], intents: list[SystemIntent], slots: list[Slot]
) -> str:
    return "\n".join([
        RULES,
        "# Scenario catalog (all scenarios)",
        "\n\n".join(_card(s) for s in sorted(scenarios, key=lambda s: s.scenario_id)),
        "\n# System intents",
        *(f"- {i.id}: {i.description} Behavior: {i.behavior}" for i in intents),
        "\n# Slots",
        *(_slot(s) for s in sorted(slots, key=lambda s: s.name)),
    ])


def output_schema(ids: list[str]) -> dict[str, Any]:
    sid = {"type": "string", "enum": ids}
    conf = {"type": "number"}

    def obj(props: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": props, "required": list(props),
                "additionalProperties": False}

    return obj({
        "scenarios": {"type": "array",
                      "items": obj({"scenario_id": sid, "confidence": conf,
                                    "reason": {"type": "string"}})},
        "alternatives": {"type": "array",
                         "items": obj({"scenario_id": sid, "confidence": conf})},
        "language": {"type": "string", "enum": ["ru", "kk", "mixed"]},
        "slots": {"type": "array",
                  "items": obj({"name": {"type": "string"}, "value": {"type": "string"}})},
        "is_continuation": {"type": "boolean"},
        "search_query": {"type": ["string", "null"]},
    })


def parse_output(raw: str) -> RouterOutput:
    data = json.loads(raw)
    data["slots"] = {s["name"]: s["value"] for s in data.get("slots") or []}
    for key in ("scenarios", "alternatives"):
        for item in data.get(key) or []:
            item["confidence"] = min(1.0, max(0.0, float(item["confidence"])))
    return RouterOutput.model_validate(data)


class OpenAIRouter:
    name = "openai"

    def __init__(self, client: Any = None, model: str | None = None):
        self._client = client
        self.model = model or settings.llm_model
        # TODO(hack): кэш на процесс, ключ — хэш каталога; правки каталога через CRUD меняют хэш.
        self._cache: tuple[str, str, list[str]] | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            if not settings.openai_api_key:
                raise RouterUnavailable(
                    "llm_unavailable",
                    "LLM-роутер не настроен: LLM_PROVIDER=openai, но OPENAI_API_KEY пуст "
                    "(см. README). Сценарий не выбран.",
                )
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=settings.openai_api_key,
                                       timeout=settings.llm_timeout_seconds, max_retries=0)
        return self._client

    async def system_prompt(self, kb: Knowledge) -> tuple[str, list[str]]:
        scenarios = await kb.catalog.scenarios()
        intents = await kb.catalog.system_intents()
        slots = await kb.catalog.slots()
        key = hashlib.sha256(json.dumps(
            [[s.model_dump() for s in scenarios], [i.model_dump() for i in intents],
             [s.model_dump() for s in slots]], sort_keys=True, default=str,
        ).encode()).hexdigest()
        if self._cache is None or self._cache[0] != key:
            ids = sorted(s.scenario_id for s in scenarios) + sorted(i.id for i in intents)
            self._cache = (key, render_system_prompt(scenarios, intents, slots), ids)
        return self._cache[1], self._cache[2]

    async def route(self, utterance: str, view: dict[str, Any], kb: Knowledge) -> RouterResult:
        client = self._get_client()
        system, ids = await self.system_prompt(kb)
        user = json.dumps({"dataset_today": DATASET_TODAY.isoformat(), "utterance": utterance,
                           "context": view}, ensure_ascii=False, default=str)
        try:
            resp = await client.responses.create(
                model=self.model,
                input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                text={"format": {"type": "json_schema", "name": "router_output", "strict": True,
                                 "schema": output_schema(ids)}},
                temperature=0,
                store=False,
            )
        except Exception as e:
            raise RouterUnavailable("llm_error", f"LLM-роутер недоступен: {type(e).__name__}") from e
        raw = resp.output_text
        try:
            out = parse_output(raw)
        except Exception as e:
            raise RouterUnavailable("llm_error", "LLM-роутер вернул невалидный JSON") from e
        usage = None
        if u := getattr(resp, "usage", None):
            usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}
            details = getattr(u, "input_tokens_details", None)
            if details is not None and getattr(details, "cached_tokens", None) is not None:
                usage["cached_input_tokens"] = details.cached_tokens
        return RouterResult(output=out, model=resp.model or self.model,
                            prompt=f"{system}\n\n# USER\n{user}", raw=raw, usage=usage)
