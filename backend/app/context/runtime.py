"""Opaque runtime slot + append-only event journal. Context does not know what lives there.

Context owns only the journal contract: board rows `type=JOURNAL`, payload with integer `seq`
and `visibility` ("public" is shown on /board), and the cursor key `CURSOR` in the slot.
Everything else (slot shape, envelope fields, reset semantics) belongs to the RuntimeOwner.
"""

from typing import Any, Protocol

from app.context.types import BoardEntry, SessionContext

JOURNAL = "kernel"  # board entry type of journal rows (persisted format)
CURSOR = "last_seq"  # slot key: last journal seq, monotonic across generations

Events = list[dict[str, Any]]


class RuntimeOwner(Protocol):
    """Registered once on Contexts by the runtime (kernel) that fills the slot."""

    active: tuple[str, Any]  # slot[key] == value marks a live runtime (idempotent create, recovery)

    def initial(self, ctx: SessionContext, spec: dict) -> tuple[dict, Events]:
        """Fresh slot for ctx's generation + events to journal (e.g. "created")."""
        ...

    def restarts_generation(self, slot: dict) -> bool:
        """Re-creating over this slot must start a new context generation."""
        ...

    def on_reset(self, before: dict, after: SessionContext) -> tuple[dict, Events]:
        """Context got a new generation: slot to keep for `after` + events to journal."""
        ...

    def entry(self, ctx: SessionContext, event: dict, seq: int) -> BoardEntry:
        """Journal row for one event; payload must carry `seq` and `visibility`."""
        ...


def journal(owner: RuntimeOwner, ctx: SessionContext, events: Events) -> list[BoardEntry]:
    entries = []
    for event in events:
        ctx.kernel[CURSOR] = ctx.kernel.get(CURSOR, 0) + 1
        entries.append(owner.entry(ctx, event, ctx.kernel[CURSOR]))
    return entries


def replace_slot(
    owner: RuntimeOwner, ctx: SessionContext, slot: dict, events: Events, cursor: int
) -> list[BoardEntry]:
    ctx.kernel = slot
    ctx.kernel[CURSOR] = cursor
    return journal(owner, ctx, events)


def reset_slot(
    owner: RuntimeOwner | None, before: SessionContext, after: SessionContext
) -> list[BoardEntry]:
    """New generation: the owner decides what survives; the journal cursor always does."""
    if not before.kernel:
        return []
    cursor = before.kernel.get(CURSOR, 0)
    if owner is None:
        after.kernel = {CURSOR: cursor}
        return []
    slot, events = owner.on_reset(before.kernel, after)
    return replace_slot(owner, after, slot, events, cursor)
