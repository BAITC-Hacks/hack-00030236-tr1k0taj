"""Kernel adapter over the existing Contexts owner and its session/board storage."""

from app.context import Contexts, PgStore, SessionNotFound
from app.db import SessionLocal


class KernelError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def event(kind, payload=None, *, author="scheduler", public=False, **fields):
    return {"type": kind, "payload": payload or {}, "author": author,
            "visibility": "public" if public else "internal", **fields}


class Repository:
    def __init__(self, sessions=SessionLocal, *, contexts: Contexts | None = None):
        self.sessions = sessions  # compatibility for isolated integration-test cleanup
        self.contexts = contexts if contexts is not None else Contexts(PgStore(sessions))

    async def create(self, agents, context, mode, session_id=None):
        return await self.contexts.kernel_create(agents, context, mode, session_id)

    async def change(self, sid, apply):
        try:
            return await self.contexts.kernel_change(sid, apply)
        except SessionNotFound:
            raise KernelError("session_not_found", "Сессия не найдена", 404) from None

    async def get(self, sid):
        try:
            return await self.contexts.kernel_get(sid)
        except SessionNotFound:
            raise KernelError("session_not_found", "Сессия не найдена", 404) from None

    async def events(self, sid, after=0, *, public=True, limit=200):
        return await self.contexts.kernel_events(sid, after, public=public, limit=limit)

    async def open_sessions(self):
        return await self.contexts.kernel_open_sessions()
