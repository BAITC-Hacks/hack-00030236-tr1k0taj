"""Contexts — единственный писатель состояния сессий (ADR 0003, 0006).

Одна сессия — один asyncio.Lock: проверка версии и запись атомарны.
Память процесса — источник истины, каждое изменение сразу уходит в store одной транзакцией.
"""

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any

from app.context.kernel import append_kernel, new_kernel, reset_kernel
from app.context.mutation import Mutation
from app.context.store import ContextStore
from app.context.types import (
    Author,
    BoardEntry,
    ContextPatch,
    EntryType,
    PatchResult,
    SessionContext,
    SessionNotFound,
)

ResetHook = Callable[[str, int], Any]  # (session_id, new_generation)


class Contexts:
    def __init__(self, store: ContextStore) -> None:
        self._store = store
        self._states: dict[str, SessionContext] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._reset_hooks: list[ResetHook] = []

    def on_reset(self, hook: ResetHook) -> None:
        """Вызывается после нового звонка / смены клиента. Фон отменяет здесь свои задачи."""
        self._reset_hooks.append(hook)

    # --- ядро ----------------------------------------------------------------

    def _state(self, session_id: str) -> SessionContext:
        try:
            return self._states[session_id]
        except KeyError:
            raise SessionNotFound(session_id) from None

    async def _load_locked(self, session_id: str) -> SessionContext:
        """Called while holding the one Contexts lock for this session."""
        if session_id not in self._states:
            state = await self._store.load(session_id)
            if state is None:
                raise SessionNotFound(session_id)
            self._states[session_id] = state
        return self._states[session_id]

    async def load(self, session_id: str) -> SessionContext:
        """Load persisted context once; return a detached snapshot."""
        async with self._locks[session_id]:
            return (await self._load_locked(session_id)).model_copy(deep=True)

    @asynccontextmanager
    async def mutate(
        self, session_id: str, *, turn_id: int | None = None, author: Author = "executor"
    ) -> AsyncIterator[Mutation]:
        """Транзакция над контекстом. Исключение внутри — ничего не записано.

        Держит lock сессии: внутри не ждём LLM и медленные чтения.
        """
        async with self._locks[session_id]:
            cur = await self._load_locked(session_id)
            m = Mutation(
                cur.model_copy(deep=True), cur.turn_id if turn_id is None else turn_id, author
            )
            yield m
            state, raw = m.finalize()
            entries = [BoardEntry(**e) for e in raw]
            if m.reset_happened:
                entries.extend(reset_kernel(cur, state))
            await self._store.save(state, entries)
            self._states[session_id] = state
        if m.reset_happened:
            for hook in self._reset_hooks:
                hook(session_id, state.generation)

    async def snapshot(self, session_id: str) -> SessionContext:
        """Копия текущего контекста. Её правка ничего не меняет."""
        return await self.load(session_id)

    # --- жизненный цикл звонка ----------------------------------------------

    async def start_call(self, session_id: str | None = None) -> SessionContext:
        """Новый звонок: чистый контекст. Для существующей сессии — новое поколение."""
        session_id = session_id or uuid.uuid4().hex
        async with self._locks[session_id]:
            before = self._states.get(session_id) or await self._store.load(session_id)
            if before is not None:
                m = Mutation(before.model_copy(deep=True), before.turn_id, "system")
                m.reset("new_call")
            else:
                state = SessionContext(session_id=session_id)
                m = Mutation(state, 0, "system")
                m.entry("call", {"status": "started", "generation": state.generation})
            state, raw = m.finalize()
            entries = [BoardEntry(**e) for e in raw]
            if before is not None:
                entries.extend(reset_kernel(before, state))
            await self._store.save(state, entries)
            self._states[session_id] = state
        if before is not None:
            for hook in self._reset_hooks:
                hook(session_id, state.generation)
        return await self.snapshot(session_id)

    async def begin_turn(
        self, session_id: str, transcript: str, language: str | None = None, **timing: int
    ) -> int:
        """Реплика клиента: новый turn_id и запись utterance. Версию не меняет."""
        async with self.mutate(session_id, author="stt") as m:
            turn_id = m.ctx.turn_id + 1
            m.turn_id = turn_id
            m.ctx.turn_id = turn_id
            m.add_turn("client", transcript, language)
            m.entry("utterance", {"text": transcript, "language": language}, **timing)
        return turn_id

    async def add_reply(
        self, session_id: str, turn_id: int, text: str, language: str | None = None
    ) -> None:
        """Ответ бота в историю и на доску. Версию не меняет."""
        async with self.mutate(session_id, turn_id=turn_id, author="executor") as m:
            m.add_turn("bot", text, language)
            m.entry("response", {"text": text, "language": language})

    async def cancel_turn(self, session_id: str, turn_id: int) -> None:
        """Кнопка «стоп»: поздние текст и аудио этого хода игнорируются (спека 7.5)."""
        async with self.mutate(session_id, turn_id=turn_id, author="user") as m:
            if turn_id not in m.ctx.cancelled_turns:
                m.ctx.cancelled_turns.append(turn_id)
            m.entry("turn", {"status": "cancelled"})

    def is_current_turn(self, session_id: str, turn_id: int) -> bool:
        s = self._state(session_id)
        return s.turn_id == turn_id and turn_id not in s.cancelled_turns

    # --- подтверждения и патчи ----------------------------------------------

    async def consume_confirmation(
        self, session_id: str, action: str, params: dict[str, Any], *, turn_id: int | None = None
    ) -> bool:
        """True ровно один раз, если action и params совпали с ожидающим подтверждением."""
        async with self.mutate(session_id, turn_id=turn_id, author="executor") as m:
            return m.consume_confirmation(action, params)

    async def apply_patch(self, patch: ContextPatch) -> PatchResult:
        """context_patch фонового помощника: проверка и запись атомарны (спека 4.5)."""
        async with self.mutate(
            patch.session_id, turn_id=patch.based_on_turn_id, author="background"
        ) as m:
            s = m.ctx
            if patch.generation != s.generation:
                status, reason = "rejected", "generation"
            elif patch.client_id != s.client_id:
                status, reason = "rejected", "client_id"
            elif patch.base_context_version != s.context_version:
                status, reason = "stale", "context_version"
            else:
                status, reason = "applied", None
                m.add_facts(patch.facts, origin="background")
            m.entry(
                "patch",
                {
                    "status": status,
                    "reason": reason,
                    "patch_generation": patch.generation,
                    "base_context_version": patch.base_context_version,
                    "facts": [f.key for f in patch.facts],
                },
            )
        return PatchResult(
            status=status,
            reason=reason,
            context_version=self._state(patch.session_id).context_version,
        )

    # --- доска ----------------------------------------------------------------

    async def log(
        self,
        session_id: str,
        turn_id: int,
        type: EntryType,
        author: Author,
        payload: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        """Незначимая запись (timing, trace, action, error). Версию не меняет.

        fields: source, source_id, confidence, ts_start_ms, ts_end_ms.
        """
        async with self.mutate(session_id, turn_id=turn_id, author=author) as m:
            m.entry(type, payload, **fields)

    async def board(
        self, session_id: str, since_turn: int | None = None, *, include_internal: bool = False
    ) -> list[BoardEntry]:
        entries = await self._store.board(session_id, since_turn)
        return [entry for entry in entries if include_internal or entry.type != "kernel"
                or entry.payload.get("visibility") == "public"]

    # --- public runtime storage API -----------------------------------------

    async def kernel_create(
        self, agents: list, context: dict, mode: str, session_id: str | None = None
    ) -> dict:
        """Attach runtime state to this call without introducing a second session writer."""
        session_id = session_id or str(uuid.uuid4())
        did_reset = False
        async with self._locks[session_id]:
            current = self._states.get(session_id) or await self._store.load(session_id)
            if current is not None and current.kernel.get("status") == "open":
                self._states[session_id] = current
                return deepcopy(current.kernel)
            state = current.model_copy(deep=True) if current else SessionContext(session_id=session_id)
            entries = []
            if state.kernel.get("status") == "closed" and state.kernel.get("messages"):
                # Reopening a manually closed call must reject cancellation-resistant old work.
                # A preceding context reset already advanced the generation and cleared messages.
                mutation = Mutation(state, state.turn_id, "system")
                mutation.reset("new_call")
                state, raw = mutation.finalize()
                entries = [BoardEntry(**entry) for entry in raw]
                entries.extend(reset_kernel(current, state))
                did_reset = True
            state.kernel = new_kernel(state, agents, context, mode)
            _, created = append_kernel(state, [{
                "type": "session.created", "payload": {"mode": mode},
                "author": "scheduler", "visibility": "public",
            }])
            await self._store.save(state, entries + created)
            self._states[session_id] = state
            result = deepcopy(state.kernel)
        if did_reset:
            for hook in self._reset_hooks:
                hook(session_id, state.generation)
        return result

    async def kernel_get(self, session_id: str) -> dict:
        async with self._locks[session_id]:
            state = await self._load_locked(session_id)
            if not state.kernel:
                raise SessionNotFound(session_id)
            return deepcopy(state.kernel)

    async def kernel_change(self, session_id: str, apply: Callable) -> tuple[Any, list[dict]]:
        """Pure synchronous mutation + event append, committed under the shared context lock."""
        async with self._locks[session_id]:
            current = await self._load_locked(session_id)
            if not current.kernel:
                raise SessionNotFound(session_id)
            state = current.model_copy(deep=True)
            sequence = state.kernel["last_seq"]
            result, pending = apply(state.kernel, sequence)
            if state.kernel["generation"] != state.generation:
                raise ValueError("Runtime generation must match its owning context")
            state.kernel["last_seq"] = sequence
            envelopes, entries = append_kernel(state, pending)
            await self._store.save(state, entries)
            self._states[session_id] = state
            return deepcopy(result), deepcopy(envelopes)

    async def kernel_events(
        self, session_id: str, after: int = 0, *, public: bool = True, limit: int = 200
    ) -> list[dict]:
        return await self._store.kernel_events(session_id, after, public=public, limit=limit)

    async def kernel_open_sessions(self) -> list[str]:
        return await self._store.kernel_open_sessions()
