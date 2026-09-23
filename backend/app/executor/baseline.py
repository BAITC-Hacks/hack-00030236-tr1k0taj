"""Минимальный исполнитель без чтений (ADR 0008): тема, стек, слоты, handoff.

Оператор — мок с контекстом. Боевой исполнитель с чтениями подключается в этом модуле.
"""

from typing import ClassVar

from app.context import SessionContext, handoff_summary
from app.executor.ports import ActionCall, Execution, ReplyBrief, ReplyLanguage, TurnInput
from app.knowledge import Knowledge

HANDOFF_TEXT = {
    "ru": "Соединяю вас с оператором и передаю детали разговора, повторять ничего не придётся.",
    "kk": "Сізді операторға қосамын, әңгіме мәліметтерін беремін, қайталаудың қажеті жоқ.",
}
MOCK_REPLY = {
    "ru": "Сценарий «{name}». Боевой исполнитель ещё не подключён, данные не запрашивались.",
    "kk": "«{name}» сценарийі. Негізгі орындаушы әлі қосылмаған, деректер сұралмады.",
}


def reply_language(language: str | None, fallback: str | None = None) -> ReplyLanguage:
    lang = language if language in ("ru", "kk") else fallback
    return "kk" if lang == "kk" else "ru"


class BaselineExecutor:
    """Минимальный исполнитель: тема, стек, слоты, handoff. Чтения — в боевом исполнителе."""

    name = "baseline"
    supported_actions: ClassVar[list[str]] = ["transfer_to_operator"]

    async def execute(self, turn: TurnInput) -> Execution:
        lang = reply_language(turn.router.language, turn.snapshot.language)
        d = turn.decision
        if d.kind == "handoff":
            return await self._handoff(turn, lang, "low_confidence")
        if d.kind == "clarify":
            unclear = next(
                i for i in await turn.kb.catalog.system_intents() if i.id == "SYS_UNCLEAR"
            )
            names = [await self._name(turn.kb, i) for i in d.clarify_options]
            names += ["что-то другое" if lang == "ru" else "басқа нәрсе"] * (2 - len(names))
            template = unclear.response[lang].format(option_a=names[0], option_b=names[1])
            return Execution(
                brief=ReplyBrief(
                    language=lang,
                    scenario_id="SYS_UNCLEAR",
                    decision="clarify",
                    instruction=f"Переспроси: {names[0]} или {names[1]}",
                    template=template,
                )
            )

        primary = d.scenarios[0].scenario_id
        if primary.startswith("SYS_"):
            intent = next(i for i in await turn.kb.catalog.system_intents() if i.id == primary)
            return Execution(
                brief=ReplyBrief(
                    language=lang,
                    scenario_id=primary,
                    decision="route",
                    instruction=intent.behavior,
                    template=intent.response.get(lang),
                )
            )

        scenario = await turn.kb.catalog.scenario(primary)
        async with turn.contexts.mutate(turn.session_id, turn_id=turn.turn_id) as m:
            # остальные темы — в стек, primary — активная (спека 2.2, стек отложенных тем)
            for s in reversed(d.scenarios[1:]):
                if not s.scenario_id.startswith("SYS_"):
                    m.switch_topic(s.scenario_id)
            m.switch_topic(primary)
            m.set_slots(primary, turn.router.slots)
        if scenario and scenario.handoff and scenario.handoff.get("when") == "always":
            return await self._handoff(turn, lang, "client_request", scenario.handoff["queue"])
        name = scenario.name if scenario else primary
        opening = (scenario.responses.get(lang) or {}).get("opening") if scenario else None
        return Execution(
            brief=ReplyBrief(
                language=lang,
                scenario_id=primary,
                decision="route",
                instruction=scenario.description if scenario else primary,
                template=opening or MOCK_REPLY[lang].format(name=name),
            )
        )

    async def _handoff(
        self, turn: TurnInput, lang: ReplyLanguage, reason: str, queue: str = "operator_general"
    ) -> Execution:
        snap: SessionContext = await turn.contexts.snapshot(turn.session_id)
        summary = handoff_summary(snap) | {"reason": reason}
        action = ActionCall(
            name="transfer_to_operator",
            mode="handoff",
            params={"queue": queue},
            result={"status": "transferred (mock)", "queue": queue, "context": summary},
        )
        return Execution(
            actions=[action],
            brief=ReplyBrief(
                language=lang,
                decision="handoff",
                instruction="Скажи, что передаёшь оператору с контекстом",
                template=HANDOFF_TEXT[lang],
                handoff=summary,
            ),
        )

    @staticmethod
    async def _name(kb: Knowledge, scenario_id: str) -> str:
        s = await kb.catalog.scenario(scenario_id)
        return s.name if s else scenario_id

