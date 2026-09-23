"""Оркестратор хода: STT → роутер → политика → исполнитель → ответ → TTS, события по ходу.

Один foreground-ход на сессию (спека 7.5). Состояние меняет только app.context.
Ошибка провайдера не роняет звонок: событие error + честный ответ (AGENTS.md).
"""

import asyncio
import base64
import hashlib
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from typing import Any
from uuid import uuid4

import anyio
from pydantic import BaseModel

from app.call.events import (
    ActionEvent,
    AudioEvent,
    ErrorEvent,
    FactsEvent,
    Latency,
    ReplyDeltaEvent,
    ReplyDoneEvent,
    RoutingEvent,
    TranscriptEvent,
    TurnCancelledEvent,
    TurnDoneEvent,
    TurnStartedEvent,
)
from app.call.journal import TERMINAL, CallJournal, fingerprint
from app.call.ports import Providers
from app.context import Contexts, SessionContext, SessionNotFound, router_view
from app.executor import Execution, ReplyBrief, TurnInput, reply_language
from app.kernel import KernelError, RecordUpdate
from app.knowledge import open_knowledge
from app.router import Decision, RouterOutput, RouterResult, UnknownScenario, decide
from app.speech import ProviderUnavailable, Transcript, split_sentences


def _ms(start: float, end: float | None = None) -> int:
    return round(((end or time.perf_counter()) - start) * 1000)


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000


class _Cancelled(Exception):
    pass


class CallService:
    def __init__(self, contexts: Contexts, providers: Providers, kernel=None) -> None:
        self.contexts = contexts
        self.providers = providers
        self.kernel = kernel
        self._speech_tasks: dict[tuple[str, int], asyncio.Task] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_router: dict[str, RouterResult] = {}
        self._opening = asyncio.Lock()
        self.journal = CallJournal(contexts)
        self._ingress_locks = defaultdict(asyncio.Lock)
        self._signals = defaultdict(asyncio.Event)
        self._producers: dict[tuple[str, str], asyncio.Task] = {}
        self._active_requests: dict[str, str] = {}
        self._request_turns: dict[tuple[str, str], int] = {}
        self._cancelled_requests: set[tuple[str, str]] = set()

    async def ensure_call(self, session_id: str) -> tuple[SessionContext, bool]:
        """Сессия по UUID с фронта: вернуть существующую или открыть новый звонок."""
        async with self._opening:  # два первых запроса с одним UUID не откроют звонок дважды
            try:
                return await self.contexts.snapshot(session_id), False
            except SessionNotFound:
                return await self.contexts.start_call(session_id), True

    def busy(self, session_id: str) -> bool:
        rid = self._active_requests.get(session_id)
        task = self._producers.get((session_id, rid))
        return self._locks[session_id].locked() or (task is not None and not task.done())

    async def cancel_turn(self, session_id: str, turn_id: int, played_ms: int | None = None):
        await self.contexts.cancel_turn(session_id, turn_id)
        if self.kernel is not None:
            await self.kernel.interrupt_turn(session_id, turn_id, played_ms)
        if task := self._speech_tasks.get((session_id, turn_id)):
            task.cancel()
        rid = self._active_requests.get(session_id)
        task = self._producers.get((session_id, rid))
        if (task is not None and task is not asyncio.current_task()
                and self._request_turns.get((session_id, rid)) == turn_id):
            task.cancel()

    async def _recover_requests(self, sid):
        generation, journal = await self.journal.state(sid)
        for rid, record in journal.get("requests", {}).items():
            task = self._producers.get((sid, rid))
            if (record["generation"] == generation and record["status"] == "running"
                    and (task is None or task.done())):
                await self.journal.append(sid, rid, ErrorEvent(
                    turn_id=record["turn_id"], stage="turn", code="request_interrupted",
                    message="Обработка запроса завершилась до получения полного ответа.", fatal=True,
                ).model_dump(mode="json"))
                self._signals[sid].set()

    async def ingress(
        self, sid, *, request_id=None, text=None, audio=None, mime="audio/webm",
        language_hint=None, task_id="default", updates=None, interrupt_previous=False,
    ):
        """Reserve once before the HTTP stream starts; retries subscribe to saved output."""
        rid = str(request_id or uuid4())
        updates = [item.model_dump(mode="json", exclude_unset=True)
                   if isinstance(item, BaseModel) else item
                   for item in (updates or [])]
        payload = {"text": text, "language_hint": language_hint, "task_id": task_id,
                   "updates": updates, "interrupt_previous": interrupt_previous}
        if audio is not None:
            payload.update(text=None, audio_sha256=hashlib.sha256(audio).hexdigest(), mime=mime)
        digest = fingerprint(payload)
        async with self._ingress_locks[sid]:
            await self.ensure_call(sid)
            await self._recover_requests(sid)
            if await self.journal.lookup(sid, rid, digest) is not None:
                return rid, False
            if self.busy(sid):
                if not interrupt_previous:
                    raise KernelError("turn_in_progress", "Предыдущий ход ещё идёт")
                previous = self._active_requests.get(sid)
                previous_task = self._producers.get((sid, previous))
                previous_turn = self._request_turns.get((sid, previous), 0)
                if previous_turn:
                    await self.cancel_turn(sid, previous_turn)
                if previous_task is None:
                    raise KernelError("turn_in_progress", "Предыдущий ход ещё завершается")
                stopped = await self._cancel_request(sid, previous)
                if not stopped or self._locks[sid].locked():
                    raise KernelError("turn_in_progress", "Предыдущий ход ещё завершается")
            await self.journal.begin(sid, rid, digest)
            self._active_requests[sid] = rid
            self._producers[(sid, rid)] = asyncio.create_task(self._produce(
                sid, rid, text=text, audio=audio, mime=mime, language_hint=language_hint,
                task_id=task_id, updates=updates, interrupt_previous=interrupt_previous,
            ))
        return rid, True

    async def _cancel_request(self, sid, rid):
        self._cancelled_requests.add((sid, rid))
        task = self._producers.get((sid, rid))
        if turn_id := self._request_turns.get((sid, rid), 0):
            await self.cancel_turn(sid, turn_id)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=1)
        record = await self.journal.lookup(sid, rid)
        if record is not None and record["status"] == "running":
            # A task cancelled before its first instruction cannot execute its own finally block.
            await self.journal.append(sid, rid, TurnCancelledEvent(
                turn_id=record["turn_id"],
            ).model_dump(mode="json"))
            self._signals[sid].set()
        return task is None or task.done()

    async def shutdown(self):
        for sid, rid in list(self._producers):
            with suppress(KernelError):
                await self._cancel_request(sid, rid)

    async def _produce(self, sid, rid, **kwargs):
        status = "cancelled"
        try:
            async with aclosing(self.run_turn(sid, request_id=rid, **kwargs)) as stream:
                async for item in stream:
                    self._request_turns[(sid, rid)] = item.turn_id
                    if (sid, rid) in self._cancelled_requests:
                        if item.turn_id:
                            await self.cancel_turn(sid, item.turn_id)
                        break
                    await self.journal.append(sid, rid, item.model_dump(mode="json"))
                    self._signals[sid].set()
                    if item.type == "turn.done":
                        status = "completed"
                    elif item.type == "error" and item.fatal:
                        status = "failed"
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — never persist provider internals in public replay
            status = "failed"
            with anyio.CancelScope(shield=True):
                await self.journal.append(sid, rid, ErrorEvent(
                    turn_id=self._request_turns.get((sid, rid), 0), stage="turn",
                    code=exc.code if isinstance(exc, KernelError) else "call_failed",
                    message="Не удалось завершить ход. Отправьте новую реплику.", fatal=True,
                ).model_dump(mode="json"))
        finally:
            with anyio.CancelScope(shield=True):
                if status == "cancelled":
                    await self.journal.append(sid, rid, TurnCancelledEvent(
                        turn_id=self._request_turns.get((sid, rid), 0),
                    ).model_dump(mode="json"))
                await self.journal.finish(sid, rid, status)
                self._signals[sid].set()

    async def replay(self, sid, *, after=0, request_id=None, cancel_on_disconnect=False):
        """Finite once the selected requests finish; reconnect never starts a provider."""
        cursor, finished = after, False
        try:
            while True:
                self._signals[sid].clear()
                batch = await self.journal.events(sid, cursor, request_id=request_id)
                for item in batch:
                    cursor = item["event_seq"]
                    yield item
                if len(batch) == 200:
                    continue
                generation, journal = await self.journal.state(sid)
                records = [record for rid, record in journal.get("requests", {}).items()
                           if record["generation"] == generation
                           and (request_id is None or request_id == rid)]
                if (all(record["status"] in TERMINAL for record in records)
                        and all(cursor >= record["last_seq"] for record in records)):
                    finished = True
                    return
                try:
                    await asyncio.wait_for(self._signals[sid].wait(), 1)
                except TimeoutError:
                    yield None
        finally:
            if not finished and cancel_on_disconnect and request_id is not None:
                task = self._producers.get((sid, request_id))
                if task is not None and not task.done():
                    with anyio.CancelScope(shield=True):
                        await self._cancel_request(sid, request_id)

    async def prepare_replay(self, sid, after=0, request_id=None):
        async with self._ingress_locks[sid]:
            await self._recover_requests(sid)
            await self.journal.validate_cursor(sid, after, request_id)

    async def run_turn(
        self,
        session_id: str,
        *,
        text: str | None = None,
        audio: bytes | None = None,
        mime: str = "audio/webm",
        language_hint: str | None = None,
        request_id=None,
        task_id="default",
        updates=None,
        interrupt_previous=False,
    ) -> AsyncIterator[BaseModel]:
        lock = self._locks[session_id]
        if lock.locked():  # API проверяет это заранее (409); здесь — защита от гонки
            yield ErrorEvent(
                turn_id=0, stage="turn", code="turn_in_progress", message="Идёт ход", fatal=True
            )
            return
        async with lock:
            turn = _Turn(self, session_id, request_id=request_id, task_id=task_id,
                         updates=updates, interrupt_previous=interrupt_previous)
            try:
                async for event in turn.run(text, audio, mime, language_hint):
                    yield event
            finally:
                if not turn.finished and turn.turn_id:
                    # клиент оборвал поток: ход неактуален, поздние события не нужны
                    with anyio.CancelScope(shield=True):
                        await self.cancel_turn(session_id, turn.turn_id)


class _Turn:
    """Состояние одного хода: тайминги и то, что пойдёт в trace."""

    def __init__(self, svc: CallService, session_id: str, *, request_id=None,
                 task_id="default", updates=None, interrupt_previous=False) -> None:
        self.svc = svc
        self.p = svc.providers
        self.ctx = svc.contexts
        self.sid = session_id
        self.turn_id = 0
        self.finished = False
        self.t0 = time.perf_counter()
        self.latency = Latency()
        self.stages: list[tuple[str, int, int]] = []  # (stage, start_epoch_ms, end_epoch_ms)
        self.router_out: RouterOutput | None = None
        self.decision: Decision | None = None
        self.actions: list[str] = []
        self.request_id = request_id
        self.task_id = task_id
        self.updates = list(updates or [])
        self.interrupt_previous = interrupt_previous

    def _stage(self, name: str, start: float) -> int:
        ms = _ms(start)
        end = _epoch_ms()
        self.stages.append((name, end - ms, end))
        return ms

    def _alive(self) -> None:
        if not self.ctx.is_current_turn(self.sid, self.turn_id):
            raise _Cancelled

    async def run(
        self, text: str | None, audio: bytes | None, mime: str, hint: str | None
    ) -> AsyncIterator[BaseModel]:
        # --- STT -------------------------------------------------------------
        if audio is not None:
            t = time.perf_counter()
            try:
                tr = await self.p.stt.transcribe(audio, mime, hint)
            except ProviderUnavailable as e:
                yield ErrorEvent(turn_id=0, stage="stt", code=e.code, message=e.message, fatal=True)
                self.finished = True
                return
            except Exception:  # noqa: BLE001 — ошибка провайдера не роняет звонок
                yield ErrorEvent(
                    turn_id=0, stage="stt", code="stt_failed",
                    message="Не удалось распознать аудио. Попробуйте текстовый ввод.", fatal=True
                )
                self.finished = True
                return
            self.latency.stt = self._stage("stt", t)
            source = "stt"
        else:
            tr = Transcript(text=text or "", language=hint)
            source = "text"

        self.turn_id = await self.ctx.begin_turn(self.sid, tr.text, tr.language)
        snap = await self.ctx.snapshot(self.sid)
        yield TranscriptEvent(
            turn_id=self.turn_id, text=tr.text, language=tr.language, source=source
        )
        yield TurnStartedEvent(
            turn_id=self.turn_id,
            session_id=self.sid,
            generation=snap.generation,
            context_version=snap.context_version,
        )

        try:
            async with open_knowledge() as kb:
                # --- роутер -------------------------------------------------------
                t = time.perf_counter()
                execution: Execution | None = None
                try:
                    rr = await self.p.router.route(tr.text, router_view(snap), kb)
                    self.svc.last_router[self.sid] = rr
                    if unknown := rr.output.unknown_ids(await kb.catalog.scenario_ids()):
                        raise UnknownScenario(unknown)
                except ProviderUnavailable as e:
                    self.latency.router = self._stage("router", t)
                    yield await self._error("router", e.code, e.message)
                    execution = self._system_reply(e.message, tr.language or snap.language)
                except UnknownScenario as e:
                    self.latency.router = self._stage("router", t)
                    yield await self._error("router", "router_invalid", str(e))
                    execution = self._system_reply(
                        "Не удалось надёжно определить запрос.", tr.language or snap.language
                    )
                except Exception:  # noqa: BLE001
                    self.latency.router = self._stage("router", t)
                    yield await self._error("router", "router_failed", "Не удалось определить запрос.")
                    execution = self._system_reply(
                        "Сервис временно недоступен.", tr.language or snap.language
                    )
                else:
                    self.latency.router = self._stage("router", t)
                    self._alive()
                    yield await self._routing(rr, snap.low_confidence_streak, kb)

                    # --- исполнитель: чтения и изменения контекста -------------------
                    t = time.perf_counter()
                    try:
                        execution = await self.p.executor.execute(
                            TurnInput(
                                session_id=self.sid,
                                turn_id=self.turn_id,
                                transcript=tr.text,
                                router=rr.output,
                                decision=self.decision,
                                snapshot=snap,
                                contexts=self.ctx,
                                kb=kb,
                            )
                        )
                    except Exception:  # noqa: BLE001
                        yield await self._error("executor", "executor_failed", "Не удалось выполнить запрос.")
                        execution = self._system_reply(
                            "Не получилось выполнить запрос.", rr.output.language
                        )
                    self.latency.reads = self._stage("reads", t)

                self._alive()
                for a in execution.actions:
                    self.actions.append(f"{a.name}:{a.mode}")
                    await self.ctx.log(
                        self.sid, self.turn_id, "action", "executor", a.model_dump(mode="json")
                    )
                    yield ActionEvent(turn_id=self.turn_id, **a.model_dump())
                if execution.facts:
                    yield FactsEvent(turn_id=self.turn_id, facts=execution.facts)

            # Preserve explicit system/clarification/handoff/action replies from the executor.
            use_kernel = (
                self.svc.kernel is not None
                and self.decision is not None
                and self.decision.kind == "route"
                and execution.brief.decision == "route"
                and not (execution.brief.scenario_id or "").startswith("SYS_")
                and execution.brief.handoff is None
                and all(action.mode == "read" for action in execution.actions)
            )
            # Explicit integration updates win over extraction; never re-publish stale saved slots.
            explicit_keys = {item["key"] for item in self.updates}
            for key, value in (self.router_out.slots if self.router_out else {}).items():
                if value is not None and key not in explicit_keys:
                    self.updates.append(RecordUpdate(
                        key=key, value=value, task_id=self.task_id, source="user",
                        source_id=f"call:{self.sid}:{self.turn_id}",
                    ).model_dump(mode="json"))
            if len(self.updates) > 32:
                raise KernelError("input_update_limit", "Слишком много обновлений за одну реплику", 422)
            if self.svc.kernel is not None and not use_kernel:
                await self.svc.kernel.update_inputs(
                    self.sid, request_id=self.request_id or uuid4(), task_id=self.task_id,
                    updates=self.updates, text=tr.text, turn_id=self.turn_id,
                )
            # факты хода зафиксированы как vN — фон стартует от этого снимка (ADR 0006)
            if not use_kernel:
                self.p.background.schedule(await self.ctx.snapshot(self.sid), self.turn_id)

            # --- ответ и TTS ------------------------------------------------------
            reply = ""
            async with aclosing(self._speak(execution.brief, tr.text, use_kernel)) as speech:
                async for event in speech:
                    self._alive()
                    if isinstance(event, TurnCancelledEvent) or (
                        isinstance(event, ErrorEvent) and event.fatal
                    ):
                        self.finished = True
                        yield event
                        return
                    if isinstance(event, ReplyDoneEvent):
                        reply = event.text
                    yield event
            self._alive()
            await self.ctx.add_reply(self.sid, self.turn_id, reply, execution.brief.language)

            if self.latency.total is None:  # без TTS: до reply.done; с TTS задано первым аудио
                self.latency.total = _ms(self.t0)
            yield await self._done(tr, reply)
        except _Cancelled:
            self.finished = True
            yield TurnCancelledEvent(turn_id=self.turn_id)
            return
        self.finished = True
        await self._log_timings()

    async def _routing(self, rr: RouterResult, low_streak: int, kb: Any) -> RoutingEvent:
        out = rr.output
        self.router_out = out
        priorities = {s.scenario_id: s.priority for s in await kb.catalog.scenarios()}
        self.decision = decide(out, priorities, low_streak)
        payload = {
            "decision": self.decision.kind,
            "scenarios": [s.model_dump() for s in self.decision.scenarios],
            "alternatives": [a.model_dump() for a in out.alternatives],
            "language": out.language,
            "slots": out.slots,
            "is_continuation": out.is_continuation,
            "clarify_options": self.decision.clarify_options,
            "model": rr.model,
        }
        async with self.ctx.mutate(self.sid, turn_id=self.turn_id, author="router") as m:
            m.record_routing(payload, out.scenarios[0].confidence)
        return RoutingEvent(
            turn_id=self.turn_id,
            decision=self.decision.kind,
            scenarios=self.decision.scenarios,
            alternatives=out.alternatives,
            language=out.language,
            slots=out.slots,
            is_continuation=out.is_continuation,
            clarify_options=self.decision.clarify_options,
        )

    async def _error(self, stage: str, code: str, message: str) -> ErrorEvent:
        self._alive()
        await self.ctx.log(
            self.sid,
            self.turn_id,
            "error",
            "system",
            {"stage": stage, "code": code, "message": message},
        )
        return ErrorEvent(
            turn_id=self.turn_id, stage=stage, code=code, message=message, fatal=False
        )

    @staticmethod
    def _system_reply(message: str, language: str | None) -> Execution:
        return Execution(
            brief=ReplyBrief(
                language=reply_language(language), instruction=message, template=message
            )
        )

    async def _speak(
        self, brief: ReplyBrief, transcript: str = "", use_kernel: bool = False
    ) -> AsyncIterator[BaseModel]:
        """Текст кусками + TTS по предложениям параллельно. Порядок аудио сохраняется."""
        out: asyncio.Queue = asyncio.Queue()
        sentences: asyncio.Queue = asyncio.Queue()
        t = time.perf_counter()

        async def text_producer() -> None:
            if use_kernel:
                await kernel_text_producer()
                return
            full, buf, first = "", "", True
            async for delta in self.p.responder.stream(brief):
                if first:
                    self.latency.response_first_token = _ms(t)
                    first = False
                full += delta
                buf += delta
                *done, buf = split_sentences(buf)
                for s in done:
                    await sentences.put((s, None, None))
                await out.put(ReplyDeltaEvent(turn_id=self.turn_id, text=delta))
            if buf.strip():
                await sentences.put((buf, None, None))
            await sentences.put(None)
            self.latency.response = self._stage("response", t)
            await out.put(ReplyDoneEvent(turn_id=self.turn_id, text=full, language=brief.language))

        async def kernel_text_producer() -> None:
            full, first, response_id = "", True, None
            segment_text: dict[str, str] = {}
            snapshot = await self.ctx.snapshot(self.sid)
            context = brief.model_dump(mode="json") | {
                "generation": snapshot.generation,
                "context_version": snapshot.context_version,
                "client_id": snapshot.client_id,
                "search_query": getattr(self.router_out, "search_query", None),
            }
            completed = False
            async for envelope in self.svc.kernel.stream_turn(
                self.sid, self.turn_id, transcript, context,
                request_id=self.request_id, task_id=self.task_id, updates=self.updates,
                interrupt_previous=self.interrupt_previous,
            ):
                self._alive()
                kind, payload = envelope["type"], envelope.get("payload", {})
                response_id = envelope.get("response_id") or response_id
                segment_id = envelope.get("segment_id")
                if kind == "response.delta":
                    delta = payload["text"]
                    if first:
                        self.latency.response_first_token = _ms(t)
                        first = False
                    full += delta
                    if segment_id:
                        segment_text[segment_id] = segment_text.get(segment_id, "") + delta
                    await out.put(ReplyDeltaEvent(
                        turn_id=self.turn_id, text=delta,
                        response_id=response_id, segment_id=segment_id,
                    ))
                elif kind == "segment.completed":
                    if sentence := segment_text.get(segment_id, "").strip():
                        await sentences.put((sentence, response_id, segment_id))
                elif kind == "response.interrupted":
                    raise _Cancelled
                elif kind == "response.failed":
                    raise ProviderUnavailable(
                        "responder", payload.get("code", "generation_failed"),
                        payload.get("message", "Не удалось завершить ответ."),
                    )
                elif kind == "response.completed":
                    completed = True
                    break
                # Internal events and control metadata never enter the client text.
            if not completed:
                raise ProviderUnavailable(
                    "responder", "stream_incomplete", "Поток ответа завершился преждевременно."
                )
            await sentences.put(None)
            self.latency.response = self._stage("response", t)
            await out.put(ReplyDoneEvent(
                turn_id=self.turn_id, text=full, language=brief.language,
                response_id=response_id,
            ))

        async def audio_producer() -> None:
            seq = 0
            while (job := await sentences.get()) is not None:
                sentence, response_id, segment_id = job
                try:
                    chunk = await self.p.tts.synthesize(sentence.strip(), brief.language)
                except Exception as e:  # noqa: BLE001
                    await out.put(await self._error("tts", "tts_failed", str(e)))
                    continue
                if chunk is None:
                    continue
                if seq == 0:
                    self.latency.tts_first_audio = self._stage("tts_first_audio", t)
                    self.latency.total = _ms(self.t0)
                await out.put(
                    AudioEvent(
                        turn_id=self.turn_id,
                        seq=seq,
                        mime=chunk.mime,
                        data=base64.b64encode(chunk.data).decode(),
                        text=sentence.strip(),
                        response_id=response_id,
                        segment_id=segment_id,
                    )
                )
                seq += 1

        async def run_all() -> None:
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(text_producer())
                    tg.create_task(audio_producer())
            except* _Cancelled:
                await out.put(TurnCancelledEvent(turn_id=self.turn_id))
            except* Exception as eg:  # noqa: BLE001 — ошибка ответа не роняет звонок
                error = eg.exceptions[0]
                await out.put(
                    ErrorEvent(
                        turn_id=self.turn_id,
                        stage="responder",
                        code=error.code if isinstance(error, ProviderUnavailable)
                        else "responder_failed",
                        message=error.message if isinstance(error, ProviderUnavailable)
                        else "Не удалось завершить ответ.",
                        fatal=True,
                    )
                )
            finally:
                await out.put(None)

        task = asyncio.create_task(run_all())
        self.svc._speech_tasks[(self.sid, self.turn_id)] = task
        finished = False
        try:
            while (event := await out.get()) is not None:
                yield event
            finished = True
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.svc._speech_tasks.pop((self.sid, self.turn_id), None)
            if not finished and not self.finished and use_kernel:
                with anyio.CancelScope(shield=True):
                    await self.svc.kernel.interrupt_turn(self.sid, self.turn_id, None)

    async def _done(self, tr: Transcript, reply: str) -> TurnDoneEvent:
        snap = await self.ctx.snapshot(self.sid)
        out = self.router_out
        scenarios = self.decision.scenarios if self.decision else []
        return TurnDoneEvent(
            turn_id=self.turn_id,
            transcript=tr.text,
            language=out.language if out else tr.language,
            scenarios=scenarios,
            alternatives=out.alternatives if out else [],
            reason="; ".join(s.reason for s in scenarios if s.reason),
            slots=out.slots if out else {},
            actions=self.actions,
            reply=reply,
            latency_ms=self.latency,
            context_version=snap.context_version,
        )

    async def _log_timings(self) -> None:
        """Каждый этап — отдельная запись timing на доске; версию не меняет."""
        for stage, start, end in self.stages:
            await self.ctx.log(
                self.sid,
                self.turn_id,
                "timing",
                "system",
                {"stage": stage, "ms": end - start},
                ts_start_ms=start,
                ts_end_ms=end,
            )
        await self.ctx.log(
            self.sid, self.turn_id, "trace", "system", {"latency_ms": self.latency.model_dump()}
        )
