"""Модуль context: состояние звонка и доска. Kernel импортирует только отсюда.

Спека: docs/specs/context-module.md
"""

from app.context.api import get_contexts
from app.context.api import router as api_router
from app.context.models import SessionRow as SessionRecord
from app.context.mutation import Mutation
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
    "SessionContext",
    "SessionNotFound",
    "SessionRecord",
    "Turn",
    "api_router",
    "get_contexts",
    "handoff_summary",
    "router_view",
]
