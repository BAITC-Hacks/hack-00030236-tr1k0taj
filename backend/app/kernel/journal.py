"""Kernel side of the context runtime slot: slot shape, envelopes, reset semantics.

Registered on Contexts via `attach_runtime`; context itself stays kernel-agnostic.
"""

from copy import deepcopy
from datetime import UTC, datetime
from time import time_ns
from uuid import uuid4

from app.context import BoardEntry, SessionContext, ensure_blackboard


class KernelJournal:
    active = ("status", "open")

    def initial(self, ctx: SessionContext, spec: dict) -> tuple[dict, list[dict]]:
        slot = self._fresh(ctx, spec["agents"], spec["context"], spec["mode"])
        return slot, [{"type": "session.created", "payload": {"mode": spec["mode"]},
                       "author": "scheduler", "visibility": "public"}]

    def restarts_generation(self, slot: dict) -> bool:
        # Reopening a manually closed call must reject cancellation-resistant old work.
        # A preceding context reset already advanced the generation and cleared messages.
        return slot.get("status") == "closed" and bool(slot.get("messages"))

    def on_reset(self, before: dict, after: SessionContext) -> tuple[dict, list[dict]]:
        """A new generation has no prior runtime context."""
        slot = self._fresh(after, before["agents"], {}, before["mode"])
        slot["status"] = "closed"
        return slot, [{"type": "session.closed", "payload": {"reason": "context_reset"},
                       "author": "scheduler", "visibility": "public"}]

    def entry(self, ctx: SessionContext, event: dict, seq: int) -> BoardEntry:
        envelope = {
            "event_id": str(uuid4()),
            "session_id": ctx.session_id,
            "seq": seq,
            "generation": ctx.generation,
            "input_revision": ctx.kernel["input_revision"],
            "turn_id": None,
            "response_id": None,
            "segment_id": None,
            "caused_by": None,
            "created_at": datetime.now(UTC).isoformat(),
            **deepcopy(event),
        }
        now = time_ns() // 1_000_000
        return BoardEntry(
            session_id=ctx.session_id,
            generation=ctx.generation,
            turn_id=envelope.get("turn_id") or ctx.turn_id,
            type="kernel",
            author="scheduler",
            payload=envelope,
            ts_start_ms=now,
            ts_end_ms=now,
            context_version=ctx.context_version,
        )

    @staticmethod
    def _fresh(ctx: SessionContext, agents: list, context: dict, mode: str) -> dict:
        slot = {
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "input_revision": 0,
            "status": "open",
            "agents": deepcopy(agents),
            "context": deepcopy(context),
            "mode": mode,
            "next_turn_id": max(1, ctx.turn_id + 1),
            "active_response_id": None,
            "messages": [],
            "responses": {},
            "runs": {},
            "background": [],
            "requests": {},
        }
        ensure_blackboard(slot)
        return slot
