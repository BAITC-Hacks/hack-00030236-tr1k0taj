"""Модуль executor: исполнитель сценариев, генерация ответа, фон (docs/specs/executor-module.md).

Агентный слой: может импортировать app.router, app.context, app.knowledge (только их публичный API).
"""

from app.executor.background import NoopBackground
from app.executor.baseline import BaselineExecutor, reply_language
from app.executor.ports import (
    ActionCall,
    ActionMode,
    Background,
    Execution,
    Executor,
    ReplyBrief,
    ReplyLanguage,
    Responder,
    TurnInput,
)
from app.executor.responder import TemplateResponder

__all__ = [
    "ActionCall",
    "ActionMode",
    "Background",
    "BaselineExecutor",
    "Execution",
    "Executor",
    "NoopBackground",
    "ReplyBrief",
    "ReplyLanguage",
    "Responder",
    "TemplateResponder",
    "TurnInput",
    "reply_language",
]
