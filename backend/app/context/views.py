"""Чистые функции над снимком: что видят роутер и оператор."""

from copy import deepcopy
from typing import Any

from app.context.blackboard import ensure_blackboard
from app.context.history import conversation_history
from app.context.types import SessionContext


def router_view(snap: SessionContext, last_turns: int = 4) -> dict[str, Any]:
    """Переменная часть промпта роутера: проверенный контекст и последние ходы (спека 5.1)."""
    topics = [t for t in [snap.active_scenario, *reversed(snap.pending_topics)] if t]
    pc = snap.pending_confirmation
    return {
        "client_identified": snap.client_id is not None,
        "active_scenario": snap.active_scenario,
        "pending_topics": list(snap.pending_topics),
        "known_slots": {t: snap.slots_by_topic[t] for t in topics if snap.slots_by_topic.get(t)},
        "pending_confirmation": {"action": pc.action, "params": pc.params} if pc else None,
        "facts": [{"key": f.key, "value": f.value, "source": f.source,
                   "source_id": f.source_id} for f in snap.facts],
        "recent_turns": conversation_history(snap)[-last_turns:],
        "active_task_id": ensure_blackboard(deepcopy(snap.kernel))["active_task_id"],
    }


def handoff_summary(snap: SessionContext) -> dict[str, Any]:
    """Контекст для оператора, чтобы клиент не повторял (спека 6.5)."""
    return {
        "session_id": snap.session_id,
        "client_id": snap.client_id,
        "language": snap.language,
        "active_scenario": snap.active_scenario,
        "pending_topics": list(snap.pending_topics),
        "slots_by_topic": snap.slots_by_topic,
        "pending_confirmation": snap.pending_confirmation.model_dump()
        if snap.pending_confirmation
        else None,
        "facts": [f.model_dump(exclude={"context_version"}) for f in snap.facts],
        "client_said": [item["text"] for item in conversation_history(snap)
                        if item["role"] == "user"],
        "conversation_history": conversation_history(snap),
        "low_confidence_streak": snap.low_confidence_streak,
    }
