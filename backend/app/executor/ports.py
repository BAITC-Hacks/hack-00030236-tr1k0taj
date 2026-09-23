"""Порты исполнителя, генератора ответа и фона; типы их входа и выхода."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, BeforeValidator

from app.context import Contexts, Fact, SessionContext
from app.knowledge import Knowledge
from app.router import Decision, RouterOutput

ReplyLanguage = Literal["ru", "kk"]


def _as_fact(value: Any) -> Any:
    """knowledge.Fact и context.Fact — разные классы с одинаковыми полями: принимаем оба."""
    return value.model_dump() if isinstance(value, BaseModel) and not isinstance(value, Fact) else value


SourcedFact = Annotated[Fact, BeforeValidator(_as_fact)]
ActionMode = Literal["read", "preview", "execute", "handoff", "unsupported"]


class ActionCall(BaseModel):
    """Вызов действия из actions.json так, как его показывает панель и README кита."""

    name: str
    mode: ActionMode
    params: dict[str, Any] = {}
    ok: bool = True
    result: Any = None
    error: dict[str, str] | None = None  # {"code": "not_found", "message": "..."}


class ReplyBrief(BaseModel):
    """Всё, что нужно генератору ответа. Факты — только из данных, с источником."""

    language: ReplyLanguage
    scenario_id: str | None = None
    decision: str | None = None
    instruction: str  # что сказать по смыслу, для LLM
    template: str | None = None  # ориентир из responses кита / системных намерений
    facts: list[SourcedFact] = []
    handoff: dict[str, Any] | None = None


class Execution(BaseModel):
    actions: list[ActionCall] = []
    facts: list[SourcedFact] = []
    brief: ReplyBrief


@dataclass
class TurnInput:
    """Вход исполнителя: решение роутера + доступ к контексту и данным."""

    session_id: str
    turn_id: int
    transcript: str
    router: RouterOutput
    decision: Decision
    snapshot: SessionContext  # снимок до изменений этого хода
    contexts: Contexts  # единственный писатель состояния
    kb: Knowledge


class Executor(Protocol):
    name: str
    supported_actions: list[str]

    async def execute(self, turn: TurnInput) -> Execution: ...


class Responder(Protocol):
    name: str

    def stream(self, brief: ReplyBrief) -> AsyncIterator[str]: ...


class Background(Protocol):
    name: str

    def schedule(self, snapshot: SessionContext, turn_id: int) -> None: ...
