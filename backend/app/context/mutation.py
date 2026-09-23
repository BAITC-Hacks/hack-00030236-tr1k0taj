"""Mutation — транзакция над рабочей копией контекста.

Все изменения идут через неё. Значимые операции помечают `dirty`: на коммите версия растёт ровно на 1.
Логирующие операции (utterance, routing, timing) версию не трогают.
"""

import time
from copy import deepcopy
from typing import Any

from app.context.blackboard import MAX_TASKS, _task_id
from app.context.types import (
    Author,
    EntryType,
    Fact,
    Origin,
    PendingConfirmation,
    SessionContext,
    Turn,
)

LOW_CONFIDENCE = 0.45
HISTORY_LIMIT = 20
TASK_FIELDS = {"active_scenario", "pending_topics", "slots_by_topic", "pending_confirmation",
               "facts", "low_confidence_streak"}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Mutation:
    def __init__(self, state: SessionContext, turn_id: int, author: Author) -> None:
        self._s = state
        self.turn_id = turn_id
        self.author = author
        self.dirty = False
        self.reset_happened = False
        self._new_facts: list[Fact] = []
        self._entries: list[dict[str, Any]] = []

    @property
    def ctx(self) -> SessionContext:
        """Рабочая копия. Читать можно, менять — только методами Mutation."""
        return self._s

    # --- служебное -------------------------------------------------------

    def entry(
        self,
        type: EntryType,
        payload: dict[str, Any] | None = None,
        *,
        author: Author | None = None,
        turn_id: int | None = None,
        source: str | None = None,
        source_id: str | None = None,
        confidence: float | None = None,
        ts_start_ms: int | None = None,
        ts_end_ms: int | None = None,
    ) -> None:
        ts = now_ms()
        self._entries.append(
            {
                "session_id": self._s.session_id,
                "generation": self._s.generation,
                "turn_id": self.turn_id if turn_id is None else turn_id,
                "type": type,
                "author": author or self.author,
                "payload": payload or {},
                "source": source,
                "source_id": source_id,
                "confidence": confidence,
                "ts_start_ms": ts_start_ms if ts_start_ms is not None else ts,
                "ts_end_ms": ts_end_ms if ts_end_ms is not None else ts,
            }
        )

    def _changed(self) -> None:
        self.dirty = True

    def _topic_stack_entry(self) -> None:
        self.entry(
            "topic_stack",
            {"active": self._s.active_scenario, "pending": list(self._s.pending_topics)},
        )

    def _drop_confirmation(self, status: str) -> None:
        pc = self._s.pending_confirmation
        if pc is None:
            return
        self._s.pending_confirmation = None
        self._changed()
        self.entry("confirmation", {"status": status, "action": pc.action, "params": pc.params})

    def reset(self, reason: str) -> None:
        """Новое поколение: история звонка не протекает (ADR 0003)."""
        old = self._s
        keep = (
            [t for t in old.history if t.turn_id == old.turn_id]
            if reason == "client_changed"
            else []
        )
        self._s = SessionContext(
            session_id=old.session_id,
            generation=old.generation + 1,
            context_version=old.context_version,  # версия монотонна через поколения
            turn_id=old.turn_id,
            language=old.language,
            history=keep,
            domain_task_id=old.domain_task_id if reason == "client_changed" else "default",
            call_journal=deepcopy(old.call_journal),
        )
        self._new_facts.clear()
        self.reset_happened = True
        self._changed()
        self.entry("call", {"status": "reset", "reason": reason, "generation": self._s.generation})

    # --- ходы (без версии) ------------------------------------------------

    def add_turn(self, role: str, text: str, language: str | None) -> None:
        self._s.history.append(Turn(turn_id=self.turn_id, role=role, text=text, language=language,
                                    task_id=self._s.domain_task_id))
        del self._s.history[:-HISTORY_LIMIT]
        if role == "client" and language:
            self._s.language = language

    def record_routing(self, payload: dict[str, Any], confidence: float | None) -> None:
        """Решение роутера на доску + счётчик низкой уверенности. Версию не меняет."""
        if confidence is not None:
            low = confidence < LOW_CONFIDENCE
            self._s.low_confidence_streak = self._s.low_confidence_streak + 1 if low else 0
        self.entry("routing", payload, confidence=confidence)

    # --- значимые изменения ----------------------------------------------

    def focus_task(self, task_id: str) -> None:
        """Switch the domain adapter's task without carrying slots or confirmation across tasks."""
        _task_id(task_id)
        state = self._s
        if task_id == state.domain_task_id:
            return
        if len(set(state.task_states) | {state.domain_task_id, task_id}) > MAX_TASKS:
            raise ValueError("task_limit")
        restored = SessionContext(session_id=state.session_id, **state.task_states.get(task_id, {}))
        for fact in self._new_facts:
            fact.context_version = state.context_version + 1
        state.task_states[state.domain_task_id] = state.model_dump(mode="json", include=TASK_FIELDS)
        for field in TASK_FIELDS:
            setattr(state, field, deepcopy(getattr(restored, field)))
        previous, state.domain_task_id = state.domain_task_id, task_id
        self._changed()
        self.entry("call", {"status": "task_focused", "task_id": task_id, "previous_task_id": previous})

    def set_client(self, client_id: str) -> None:
        """None→X: идентификация. X→Y: другой клиент, новое поколение."""
        prev = self._s.client_id
        if prev == client_id:
            return
        if prev is not None:
            self.reset("client_changed")
        self._s.client_id = client_id
        self._changed()
        self.entry("client", {"client_id": client_id, "previous": prev})

    def switch_topic(self, scenario_id: str) -> None:
        """Новая тема становится активной, текущая уходит в стек."""
        s = self._s
        if s.active_scenario == scenario_id:
            return
        if s.active_scenario is not None:
            if s.active_scenario in s.pending_topics:
                s.pending_topics.remove(s.active_scenario)
            s.pending_topics.append(s.active_scenario)
        if scenario_id in s.pending_topics:
            s.pending_topics.remove(scenario_id)
        s.active_scenario = scenario_id
        if s.pending_confirmation and s.pending_confirmation.topic != scenario_id:
            self._drop_confirmation("topic_changed")
        self._changed()
        self._topic_stack_entry()

    def finish_topic(self) -> str | None:
        """Закрыть активную тему. Возвращает тему, к которой стоит предложить вернуться."""
        s = self._s
        if s.active_scenario is None:
            return s.pending_topics[-1] if s.pending_topics else None
        s.active_scenario = None
        self._drop_confirmation("topic_finished")
        self._changed()
        self._topic_stack_entry()
        return s.pending_topics[-1] if s.pending_topics else None

    def resume_topic(self) -> str | None:
        """Снять вершину стека и сделать активной. Текущая активная тема отбрасывается."""
        s = self._s
        if not s.pending_topics:
            return None
        top = s.pending_topics.pop()
        if s.pending_confirmation and s.pending_confirmation.topic != top:
            self._drop_confirmation("topic_changed")
        s.active_scenario = top
        self._changed()
        self._topic_stack_entry()
        return top

    def set_slots(self, topic: str, values: dict[str, Any]) -> None:
        """Новое значение сильнее старого. None = «не названо», не затирает."""
        cur = self._s.slots_by_topic.setdefault(topic, {})
        changed = {k: v for k, v in values.items() if v is not None and cur.get(k) != v}
        if not changed:
            return
        cur.update(changed)
        self._changed()
        self.entry("slots", {"topic": topic, "changed": changed})
        pc = self._s.pending_confirmation
        if (
            pc is not None
            and pc.topic in (None, topic)
            and any(k in pc.params and pc.params[k] != v for k, v in changed.items())
        ):
            self._drop_confirmation("params_changed")

    def add_facts(self, facts: list[Fact], origin: Origin = "foreground") -> None:
        """Факт с тем же key заменяется новым. Принимает и `knowledge.Fact` (те же поля)."""
        for f in facts:
            fact = Fact.model_validate({**f.model_dump(), "origin": origin})
            self._s.facts = [x for x in self._s.facts if x.key != fact.key] + [fact]
            self._new_facts.append(fact)
            self.entry(
                "facts",
                {"key": fact.key, "value": fact.value, "origin": origin},
                source=fact.source,
                source_id=fact.source_id,
            )
        if facts:
            self._changed()

    def request_confirmation(
        self, action: str, params: dict[str, Any], topic: str | None = None
    ) -> None:
        self._s.pending_confirmation = PendingConfirmation(
            action=action,
            params=params,
            topic=topic or self._s.active_scenario,
            turn_id=self.turn_id,
        )
        self._changed()
        self.entry("confirmation", {"status": "requested", "action": action, "params": params})

    def cancel_confirmation(self) -> None:
        self._drop_confirmation("cancelled")

    def consume_confirmation(self, action: str, params: dict[str, Any]) -> bool:
        pc = self._s.pending_confirmation
        if pc is None or pc.action != action or pc.params != params:
            self.entry("confirmation", {"status": "mismatch", "action": action, "params": params})
            return False
        self._drop_confirmation("consumed")
        return True

    # --- коммит ------------------------------------------------------------

    def finalize(self) -> tuple[SessionContext, list[dict[str, Any]]]:
        if self.dirty:
            self._s.context_version += 1
        v = self._s.context_version
        for f in self._new_facts:
            f.context_version = v
        for e in self._entries:
            e["context_version"] = v
        return self._s, self._entries
