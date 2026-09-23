"""Модуль context: состояние звонка и доска. Kernel импортирует только отсюда.

Спека: docs/specs/context-module.md
"""

from app.context.api import get_contexts
from app.context.api import router as api_router
from app.context.blackboard import (
    Blackboard,
    BlackboardRecord,
    BlackboardTask,
    ensure_blackboard,
    fingerprint_matches,
    input_versions,
    put_record,
    select_records,
    update_task,
)
from app.context.history import conversation_history, delivery, kernel_history
from app.context.models import SessionRow as SessionRecord
from app.context.mutation import Mutation
from app.context.runtime import RuntimeOwner
from app.context.service import Contexts
from app.context.store import ContextStore, MemoryStore, PgStore
from app.context.types import (
    BoardEntry,
    ContextPatch,
    Fact,
    PatchResult,
    PendingConfirmation,
    SessionContext,
    SessionNotFound,
    Turn,
)
from app.context.views import handoff_summary, router_view

__all__ = [
    "Blackboard",
    "BlackboardRecord",
    "BlackboardTask",
    "BoardEntry",
    "ContextPatch",
    "ContextStore",
    "Contexts",
    "Fact",
    "MemoryStore",
    "Mutation",
    "PatchResult",
    "PendingConfirmation",
    "PgStore",
    "RuntimeOwner",
    "SessionContext",
    "SessionNotFound",
    "SessionRecord",
    "Turn",
    "api_router",
    "conversation_history",
    "delivery",
    "ensure_blackboard",
    "fingerprint_matches",
    "get_contexts",
    "handoff_summary",
    "input_versions",
    "kernel_history",
    "put_record",
    "router_view",
    "select_records",
    "update_task",
]
