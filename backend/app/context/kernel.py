"""Runtime projection/envelope helpers. Persistence and locks belong to Contexts."""

from copy import deepcopy
from datetime import UTC, datetime
from time import time_ns
from uuid import uuid4

from app.context.types import BoardEntry, SessionContext


def new_kernel(state: SessionContext, agents: list, context: dict, mode: str) -> dict:
    return {
        "session_id": state.session_id,
        "generation": state.generation,
        "input_revision": 0,
        "status": "open",
        "agents": deepcopy(agents),
        "context": deepcopy(context),
        "mode": mode,
        "next_turn_id": max(1, state.turn_id + 1),
        "active_response_id": None,
        "messages": [],
        "responses": {},
        "runs": {},
        "background": [],
        "requests": {},
        "last_seq": state.kernel.get("last_seq", 0),
    }


def append_kernel(state: SessionContext, pending: list[dict]) -> tuple[list[dict], list[BoardEntry]]:
    envelopes, entries = [], []
    for item in pending:
        state.kernel["last_seq"] = state.kernel.get("last_seq", 0) + 1
        envelope = {
            "event_id": str(uuid4()),
            "session_id": state.session_id,
            "seq": state.kernel["last_seq"],
            "generation": state.generation,
            "input_revision": state.kernel["input_revision"],
            "turn_id": None,
            "response_id": None,
            "segment_id": None,
            "caused_by": None,
            "created_at": datetime.now(UTC).isoformat(),
            **deepcopy(item),
        }
        now = time_ns() // 1_000_000
        entries.append(BoardEntry(
            session_id=state.session_id,
            generation=state.generation,
            turn_id=envelope.get("turn_id") or state.turn_id,
            type="kernel",
            author="scheduler",
            payload=envelope,
            ts_start_ms=now,
            ts_end_ms=now,
            context_version=state.context_version,
        ))
        envelopes.append(envelope)
    return envelopes, entries


def reset_kernel(before: SessionContext, after: SessionContext) -> list[BoardEntry]:
    """A new generation has no prior runtime context, while journal cursors remain monotonic."""
    if not before.kernel:
        return []
    after.kernel = deepcopy(before.kernel)
    after.kernel = new_kernel(after, before.kernel["agents"], {}, before.kernel["mode"])
    after.kernel["status"] = "closed"
    _, entries = append_kernel(after, [{
        "type": "session.closed", "payload": {"reason": "context_reset"},
        "author": "scheduler", "visibility": "public",
    }])
    return entries
