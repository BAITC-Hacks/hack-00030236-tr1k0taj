"""Хранилище: состояние сессии + доска. Одна запись = одна транзакция.

Журнал runtime — строки доски `type=JOURNAL` с `payload.seq`/`payload.visibility`;
`append` пишет только строки доски, без upsert состояния (см. Contexts.runtime_change).
"""

from copy import deepcopy
from typing import Protocol

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.dialects.postgresql import insert

from app.context.runtime import JOURNAL
from app.context.types import BoardEntry, SessionContext


class ContextStore(Protocol):
    async def save(self, ctx: SessionContext, entries: list[BoardEntry]) -> None: ...

    async def append(self, entries: list[BoardEntry]) -> None: ...

    async def board(self, session_id: str, since_turn: int | None = None) -> list[BoardEntry]: ...

    async def load(self, session_id: str) -> SessionContext | None: ...

    async def runtime_sessions(self, key: str, value: str) -> list[str]: ...

    async def journal(
        self, session_id: str, after: int, *, public: bool, limit: int
    ) -> list[dict]: ...

    async def journal_cursor(self, session_id: str) -> int: ...


class MemoryStore:
    """Для тестов и режима без БД."""

    def __init__(self) -> None:
        self.states: dict[str, SessionContext] = {}
        self.entries: list[BoardEntry] = []

    async def save(self, ctx: SessionContext, entries: list[BoardEntry]) -> None:
        self.states[ctx.session_id] = ctx.model_copy(deep=True)
        self.entries.extend(e.model_copy(deep=True) for e in entries)

    async def append(self, entries: list[BoardEntry]) -> None:
        self.entries.extend(e.model_copy(deep=True) for e in entries)

    async def load(self, session_id: str) -> SessionContext | None:
        state = self.states.get(session_id)
        return state.model_copy(deep=True) if state is not None else None

    async def runtime_sessions(self, key: str, value: str) -> list[str]:
        return [sid for sid, state in self.states.items() if state.kernel.get(key) == value]

    async def journal(
        self, session_id: str, after: int, *, public: bool, limit: int
    ) -> list[dict]:
        return [deepcopy(entry.payload) for entry in self.entries
                if entry.session_id == session_id and entry.type == JOURNAL
                and entry.payload["seq"] > after
                and (not public or entry.payload["visibility"] == "public")][:limit]

    async def journal_cursor(self, session_id: str) -> int:
        return max((entry.payload["seq"] for entry in self.entries
                    if entry.session_id == session_id and entry.type == JOURNAL), default=0)

    async def board(self, session_id: str, since_turn: int | None = None) -> list[BoardEntry]:
        return [
            e
            for e in self.entries
            if e.session_id == session_id and (since_turn is None or e.turn_id >= since_turn)
        ]


class PgStore:
    def __init__(self, session_factory=None) -> None:
        if session_factory is None:
            from app.db import SessionLocal

            session_factory = SessionLocal
        self._sf = session_factory

    async def save(self, ctx: SessionContext, entries: list[BoardEntry]) -> None:
        from app.context.models import BoardEntryRow, SessionRow

        state = ctx.model_dump(mode="json")
        state["kernel"] = deepcopy(ctx.kernel)
        state["call_journal"] = deepcopy(ctx.call_journal)
        state["task_states"] = deepcopy(ctx.task_states)
        async with self._sf() as db, db.begin():
            stmt = insert(SessionRow).values(
                session_id=ctx.session_id,
                generation=ctx.generation,
                context_version=ctx.context_version,
                state=state,
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[SessionRow.session_id],
                    set_={
                        "generation": ctx.generation,
                        "context_version": ctx.context_version,
                        "state": state,
                    },
                )
            )
            db.add_all(BoardEntryRow(**e.model_dump(mode="json")) for e in entries)

    async def append(self, entries: list[BoardEntry]) -> None:
        from app.context.models import BoardEntryRow

        if not entries:
            return
        async with self._sf() as db, db.begin():
            db.add_all(BoardEntryRow(**e.model_dump(mode="json")) for e in entries)

    async def load(self, session_id: str) -> SessionContext | None:
        from app.context.models import SessionRow

        async with self._sf() as db:
            row = await db.get(SessionRow, session_id)
            return SessionContext.model_validate(row.state) if row is not None else None

    async def runtime_sessions(self, key: str, value: str) -> list[str]:
        from app.context.models import SessionRow

        async with self._sf() as db:
            query = select(SessionRow.session_id).where(
                SessionRow.state["kernel"][key].astext == value
            )
            return list(await db.scalars(query))

    async def journal(
        self, session_id: str, after: int, *, public: bool, limit: int
    ) -> list[dict]:
        from app.context.models import BoardEntryRow

        sequence = cast(BoardEntryRow.payload["seq"].astext, BigInteger)
        query = select(BoardEntryRow.payload).where(
            BoardEntryRow.session_id == session_id,
            BoardEntryRow.type == JOURNAL,
            sequence > after,
        )
        if public:
            query = query.where(BoardEntryRow.payload["visibility"].astext == "public")
        async with self._sf() as db:
            return list(await db.scalars(query.order_by(sequence).limit(limit)))

    async def journal_cursor(self, session_id: str) -> int:
        from app.context.models import BoardEntryRow

        sequence = cast(BoardEntryRow.payload["seq"].astext, BigInteger)
        query = select(func.max(sequence)).where(
            BoardEntryRow.session_id == session_id, BoardEntryRow.type == JOURNAL
        )
        async with self._sf() as db:
            return (await db.scalar(query)) or 0

    async def board(self, session_id: str, since_turn: int | None = None) -> list[BoardEntry]:
        from app.context.models import BoardEntryRow

        q = select(BoardEntryRow).where(BoardEntryRow.session_id == session_id)
        if since_turn is not None:
            q = q.where(BoardEntryRow.turn_id >= since_turn)
        async with self._sf() as db:
            rows = (await db.scalars(q.order_by(BoardEntryRow.id))).all()
        return [BoardEntry.model_validate(r, from_attributes=True) for r in rows]
