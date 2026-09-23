"""Исполнитель: тема, стек, слоты, детерминированные чтения, handoff (ADR 0005).

Чтения выполняются обычным кодом по карточке сценария, без вызова модели: факты
приезжают в бриф до того, как говорящий агент начал отвечать. Оператор — мок с контекстом.
"""

from typing import ClassVar

from app.context import SessionContext, handoff_summary
from app.executor.ports import ActionCall, Execution, ReplyBrief, ReplyLanguage, TurnInput
from app.executor.titles import title
from app.knowledge import Fact, Knowledge

READ_ACTIONS = ("find_client", "get_claim", "kb_lookup", "get_offices", "check_payment")

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
    supported_actions: ClassVar[list[str]] = [
        "find_client", "get_claim", "kb_lookup", "get_offices", "check_payment",
        "transfer_to_operator",
    ]

    async def execute(self, turn: TurnInput) -> Execution:
        lang = reply_language(turn.router.language, turn.snapshot.language)
        d = turn.decision
        if d.kind == "handoff":
            return await self._handoff(turn, lang, "low_confidence")
        if d.kind == "clarify":
            unclear = next(
                i for i in await turn.kb.catalog.system_intents() if i.id == "SYS_UNCLEAR"
            )
            names = [title(i, lang) or await self._name(turn.kb, i) for i in d.clarify_options]
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
        slots = {**(turn.snapshot.slots_by_topic.get(primary) or {}), **turn.router.slots}
        actions, facts = await self._read(turn, scenario, slots)
        return Execution(
            actions=actions,
            facts=facts,
            brief=ReplyBrief(
                language=lang,
                scenario_id=primary,
                decision="route",
                instruction=scenario.description if scenario else primary,
                template=opening or MOCK_REPLY[lang].format(name=name),
                facts=facts,
            ),
        )

    async def _read(self, turn: TurnInput, scenario, slots: dict) -> tuple[list, list]:
        """Read-действия сценария обычным кодом. Каждый факт несёт source и source_id."""
        wanted = [a for a in (getattr(scenario, "actions", None) or []) if a in READ_ACTIONS]
        if not wanted:
            wanted = ["kb_lookup"] if slots.get("topic") else []
        actions: list[ActionCall] = []
        facts: list[Fact] = []
        client = None
        for action in dict.fromkeys(wanted):
            try:
                call, produced, client = await self._run_action(
                    turn, action, slots, client,
                )
            except Exception as exc:  # noqa: BLE001 — чтение не должно ронять ход
                actions.append(ActionCall(
                    name=action, mode="read", ok=False,
                    error={"code": "read_failed", "message": type(exc).__name__},
                ))
                continue
            if call is not None:
                actions.append(call)
            facts.extend(produced)
        return actions, facts

    async def _run_action(self, turn: TurnInput, action: str, slots: dict, client):
        """Возвращает (ActionCall | None, факты, найденный клиент)."""
        kb, produced = turn.kb, []
        if action == "find_client":
            if client is None:
                phone, iin = slots.get("phone"), slots.get("iin")
                if not phone and not iin:
                    return None, [], client
                client = await kb.records.find_client(phone=phone, iin=iin)
            if client is None:
                return ActionCall(name=action, mode="read", ok=False,
                                  error={"code": "not_found", "message": "клиент не найден"}), [], None
            produced = [Fact(key="client.full_name", value=client.full_name,
                             source="find_client", source_id=client.client_id),
                        Fact(key="client.city", value=client.city,
                             source="find_client", source_id=client.client_id)]
            return ActionCall(name=action, mode="read", params={"client_id": client.client_id},
                              result={"client_id": client.client_id}), produced, client

        if action == "get_claim":
            number = slots.get("claim_number")
            claim = await kb.records.claim(number) if number else None
            if claim is None and client is not None:
                found = await kb.records.claims(client.client_id)
                claim = found[0] if len(found) == 1 else None
            if claim is None:
                return None, [], client
            produced = [Fact(key="claim.status", value=claim.status,
                             source="get_claim", source_id=claim.claim_number),
                        Fact(key="claim.next_step", value=claim.next_step,
                             source="get_claim", source_id=claim.claim_number)]
            return ActionCall(name=action, mode="read", params={"claim_number": claim.claim_number},
                              result={"status": claim.status}), produced, client

        if action == "check_payment":
            if client is None:
                return None, [], client
            payments = await kb.records.payments(client.client_id, slots.get("payment_date"))
            if not payments:
                return ActionCall(name=action, mode="read", ok=False,
                                  error={"code": "not_found", "message": "платежи не найдены"}), [], client
            produced = [Fact(key="payment.status", value=f"{p.amount} / {p.status}",
                             source="check_payment", source_id=p.payment_id) for p in payments[:3]]
            return ActionCall(name=action, mode="read",
                              params={"client_id": client.client_id},
                              result={"count": len(payments)}), produced, client

        if action == "get_offices":
            city = slots.get("city") or (client.city if client else None)
            if not city:
                return None, [], client
            produced = await kb.search.offices(city)
            return ActionCall(name=action, mode="read", params={"city": city},
                              result={"count": len(produced)}), produced[:3], client

        if action == "kb_lookup":
            topic = slots.get("topic")
            if not topic:
                return None, [], client
            fact = await kb.search.kb_lookup(topic)
            if fact is None:
                return ActionCall(name=action, mode="read", ok=False,
                                  error={"code": "not_found", "message": "тема не найдена"}), [], client
            return ActionCall(name=action, mode="read", params={"topic": topic},
                              result={"topic": topic}), [fact], client

        return None, [], client

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

