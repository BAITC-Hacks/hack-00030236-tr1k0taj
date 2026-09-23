"""Public v1 kernel contracts. Browser URLs add the existing /api proxy prefix."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentSpec(Contract):
    agent_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    instructions: str = Field(min_length=1, max_length=4000)
    on: list[Literal["user.message", "agent.result"]] = Field(
        default_factory=lambda: ["user.message"]
    )
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    tools: list[Literal["rag_search", "rag_read"]] = Field(
        default_factory=lambda: ["rag_search", "rag_read"]
    )
    timeout_seconds: float = Field(default=20, ge=1, le=60)


def default_agents() -> list[AgentSpec]:
    return [
        AgentSpec(
            agent_id="knowledge",
            instructions="Найди в базе знаний факты для текущего вопроса. Укажи источники.",
        ),
        AgentSpec(
            agent_id="next_step",
            instructions=(
                "Найди в базе знаний следующий практический шаг и требования к документам "
                "по текущему вопросу. Только проверенные источники. Не выполняй действий."
            ),
        ),
    ]


class CreateSession(Contract):
    session_id: UUID | None = None
    agents: list[AgentSpec] = Field(default_factory=default_agents, max_length=8)
    context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self):
        import json

        if len(json.dumps(self.context, ensure_ascii=False)) > 16000:
            raise ValueError("context is limited to 16000 characters")
        mapping = {a.agent_id: a for a in self.agents}
        if len(mapping) != len(self.agents):
            raise ValueError("agent_id must be unique")
        if "main" in mapping:
            raise ValueError("main is reserved")
        visited, active = set(), set()

        def visit(agent_id):
            if agent_id in active:
                raise ValueError("agent dependencies must be acyclic")
            if agent_id in visited:
                return
            if agent_id not in mapping:
                raise ValueError("unknown agent dependency")
            active.add(agent_id)
            for parent in mapping[agent_id].depends_on:
                visit(parent)
            active.remove(agent_id)
            visited.add(agent_id)

        for agent in self.agents:
            visit(agent.agent_id)
            if agent.depends_on and "agent.result" not in agent.on:
                raise ValueError("dependent agents must subscribe to agent.result")
        return self


class TurnRequest(Contract):
    request_id: UUID
    text: str = Field(min_length=1, max_length=8000)

    @model_validator(mode="after")
    def not_blank(self):
        if not self.text.strip():
            raise ValueError("text cannot be blank")
        return self


class InterruptRequest(Contract):
    request_id: UUID
    response_id: UUID
    played_ms: int = Field(ge=0, le=3600000)


class SegmentTiming(Contract):
    segment_id: UUID
    start_ms: int = Field(ge=0, le=3600000)
    end_ms: int = Field(gt=0, le=3600000)

    @model_validator(mode="after")
    def positive_interval(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class PlaybackRequest(InterruptRequest):
    segments: list[SegmentTiming] = Field(default_factory=list, max_length=16)
    text_segment_ids: list[UUID] = Field(default_factory=list, max_length=16)


DocumentKind = Literal["kb", "office", "clinic", "inspection_point"]


class RAGSearchArgs(Contract):
    query: str = Field(min_length=1, max_length=1000)
    search_query: str | None = Field(default=None, min_length=1, max_length=1000)
    kinds: list[DocumentKind] = Field(default_factory=lambda: ["kb"], max_length=4)
    limit: int = Field(default=3, ge=1, le=10)


class RAGReadArgs(Contract):
    kind: DocumentKind
    key: str = Field(min_length=1, max_length=128)


class EventEnvelope(Contract):
    event_id: str
    session_id: str
    seq: int
    type: str
    visibility: Literal["public", "internal"]
    author: str
    turn_id: int | None = None
    response_id: str | None = None
    segment_id: str | None = None
    generation: int
    input_revision: int
    caused_by: str | None = None
    payload: dict[str, Any]
    created_at: str


class TurnAccepted(Contract):
    session_id: str
    turn_id: int
    response_id: str


class SessionSnapshot(Contract):
    session_id: str
    generation: int
    input_revision: int
    status: str
    last_seq: int
    active_response_id: str | None
    agents: list[str]
    mode: Literal["mock", "live"]


PositiveCursor = Annotated[int, Field(ge=0)]
