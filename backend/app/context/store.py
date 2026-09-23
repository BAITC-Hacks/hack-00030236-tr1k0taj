"""Хранилище: состояние сессии + доска. Одна запись = одна транзакция."""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.context.types import BoardEntry, SessionContext


class ContextStore(Protocol):
    async def save(self, ctx: SessionContext, entries: list[BoardEntry]) -> None: ...

    async def board(self, session_id: str, since_turn: int | None = None) -> list[BoardEntry]: ...


class MemoryStore:
    """Для тестов и режима без БД."""

    def __init__(self) -> None:
        self.states: dict[str, SessionContext] = {}
        self.entries: list[BoardEntry] = []

    async def save(self, ctx: SessionContext, entries: list[BoardEntry]) -> None:
        self.states[ctx.session_id] = ctx.model_copy(deep=True)
        self.entries.extend(entries)

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

    async def board(self, session_id: str, since_turn: int | None = None) -> list[BoardEntry]:
        from app.context.models import BoardEntryRow

        q = select(BoardEntryRow).where(BoardEntryRow.session_id == session_id)
        if since_turn is not None:
            q = q.where(BoardEntryRow.turn_id >= since_turn)
        async with self._sf() as db:
            rows = (await db.scalars(q.order_by(BoardEntryRow.id))).all()
        return [BoardEntry.model_validate(r, from_attributes=True) for r in rows]
