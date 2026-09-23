"""Atomic state projection + append-only journal. No model/network waits in transactions."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.db import SessionLocal
from app.kernel.models import KernelEvent, KernelSession


class KernelError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def event(kind, payload=None, *, author="scheduler", public=False, **fields):
    return {"type": kind, "payload": payload or {}, "author": author,
            "visibility": "public" if public else "internal", **fields}


class Repository:
    def __init__(self, sessions=SessionLocal):
        self.sessions = sessions

    async def create(self, agents, context, mode):
        sid = str(uuid4())
        state = {
            "session_id": sid, "generation": 1, "input_revision": 0, "status": "open",
            "agents": agents, "context": context, "mode": mode, "next_turn_id": 1,
            "active_response_id": None, "messages": [], "responses": {}, "runs": {},
            "background": [], "requests": {},
        }
        async with self.sessions() as db, db.begin():
            row = KernelSession(id=sid, status="open", seq=0, state=state)
            db.add(row)
            await db.flush()
            self._append(db, row, state, [event("session.created", {"mode": mode}, public=True)])
        return await self.get(sid)

    @staticmethod
    def _append(db, row, state, pending):
        output = []
        for item in pending:
            row.seq += 1
            envelope = {
                "event_id": str(uuid4()), "session_id": row.id, "seq": row.seq,
                "generation": state["generation"], "input_revision": state["input_revision"],
                "turn_id": None, "response_id": None, "segment_id": None, "caused_by": None,
                "created_at": datetime.now(UTC).isoformat(), **item,
            }
            db.add(KernelEvent(id=envelope["event_id"], session_id=row.id, seq=row.seq,
                               visibility=envelope["visibility"], envelope=envelope))
            output.append(envelope)
        return output

    async def change(self, sid, apply):
        async with self.sessions() as db, db.begin():
            row = await db.scalar(select(KernelSession).where(KernelSession.id == sid).with_for_update())
            if row is None:
                raise KernelError("session_not_found", "Сессия не найдена", 404)
            state = deepcopy(row.state)
            result, pending = apply(state, row.seq)
            emitted = self._append(db, row, state, pending)
            row.state, row.status = state, state["status"]
        return result, emitted

    async def get(self, sid):
        async with self.sessions() as db:
            row = await db.get(KernelSession, sid)
            if row is None:
                raise KernelError("session_not_found", "Сессия не найдена", 404)
            return {**deepcopy(row.state), "last_seq": row.seq}

    async def events(self, sid, after=0, *, public=True, limit=200):
        async with self.sessions() as db:
            stmt = select(KernelEvent.envelope).where(
                KernelEvent.session_id == sid, KernelEvent.seq > after
            )
            if public:
                stmt = stmt.where(KernelEvent.visibility == "public")
            return list(await db.scalars(stmt.order_by(KernelEvent.seq).limit(limit)))

    async def open_sessions(self):
        async with self.sessions() as db:
            return list(await db.scalars(select(KernelSession.id).where(KernelSession.status == "open")))
