"""Contexts — единственный писатель состояния сессий (ADR 0003, 0006).

Одна сессия — один asyncio.Lock: проверка версии и запись атомарны.
Память процесса — источник истины, каждое изменение сразу уходит в store одной транзакцией.
"""

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

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

    @asynccontextmanager
    async def mutate(
        self, session_id: str, *, turn_id: int | None = None, author: Author = "executor"
    ) -> AsyncIterator[Mutation]:
        """Транзакция над контекстом. Исключение внутри — ничего не записано.

        Держит lock сессии: внутри не ждём LLM и медленные чтения.
        """
        async with self._locks[session_id]:
            cur = self._state(session_id)
            m = Mutation(
                cur.model_copy(deep=True), cur.turn_id if turn_id is None else turn_id, author
            )
            yield m
            state, raw = m.finalize()
            await self._store.save(state, [BoardEntry(**e) for e in raw])
            self._states[session_id] = state
        if m.reset_happened:
            for hook in self._reset_hooks:
                hook(session_id, state.generation)

    async def snapshot(self, session_id: str) -> SessionContext:
        """Копия текущего контекста. Её правка ничего не меняет."""
        return self._state(session_id).model_copy(deep=True)

    # --- жизненный цикл звонка ----------------------------------------------

    async def start_call(self, session_id: str | None = None) -> SessionContext:
        """Новый звонок: чистый контекст. Для существующей сессии — новое поколение."""
        session_id = session_id or uuid.uuid4().hex
        if session_id in self._states:
            async with self.mutate(session_id, author="system") as m:
                m.reset("new_call")
        else:
            async with self._locks[session_id]:
                state = SessionContext(session_id=session_id)
                m = Mutation(state, 0, "system")
                m.entry("call", {"status": "started", "generation": state.generation})
                state, raw = m.finalize()
                await self._store.save(state, [BoardEntry(**e) for e in raw])
                self._states[session_id] = state
        return await self.snapshot(session_id)

    async def begin_turn(
        self, session_id: str, transcript: str, language: str | None = None, **timing: int
    ) -> int:
        """Реплика клиента: новый turn_id и запись utterance. Версию не меняет."""
        turn_id = self._state(session_id).turn_id + 1
        async with self.mutate(session_id, turn_id=turn_id, author="stt") as m:
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

    async def board(self, session_id: str, since_turn: int | None = None) -> list[BoardEntry]:
        return await self._store.board(session_id, since_turn)
