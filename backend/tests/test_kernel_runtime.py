"""Persisted kernel behavior with controlled providers; no external API calls."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.kernel.context import history, package
from app.kernel.models import KernelSession
from app.kernel.provider import BackgroundResult, SegmentResult
from app.kernel.runtime import Runtime
from app.kernel.store import KernelError, Repository
from app.kernel.types import (
    AgentSpec,
    CreateSession,
    InterruptRequest,
    PlaybackRequest,
    TurnRequest,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "embeddings_enabled", False)
    monkeypatch.setattr(settings, "kernel_max_segments", 4)


class ScopedRepository(Repository):
    """Recovery and cleanup must not touch another test's or developer's sessions."""

    def __init__(self, sessions):
        super().__init__(sessions)
        self.ids = set()

    async def create(self, *args, **kwargs):
        state = await super().create(*args, **kwargs)
        self.ids.add(state["session_id"])
        return state

    async def open_sessions(self):
        return [sid for sid in await super().open_sessions() if sid in self.ids]


@asynccontextmanager
async def kernel(driver):
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    repo = ScopedRepository(async_sessionmaker(engine, expire_on_commit=False))
    runtime = Runtime(driver, repository=repo)
    try:
        yield runtime
    finally:
        await runtime.shutdown()
        async with repo.sessions() as db, db.begin():
            await db.execute(delete(KernelSession).where(KernelSession.id.in_(repo.ids)))
        await engine.dispose()


class Driver:
    async def stream_segment(self, context, emit, tool):
        await emit("Ответ.")
        return SegmentResult("Ответ.")

    async def run_background(self, agent, context, tool):
        return BackgroundResult("INTERNAL_ONLY", source_ids=["kb:claims.submission"])

    async def close(self):
        pass


async def completed(task):
    return await asyncio.wait_for(asyncio.shield(task), timeout=5)


def turn(text="Что с заявлением?"):
    return TurnRequest(request_id=uuid4(), text=text)


def test_two_background_results_reach_next_segment_of_same_response():
    async def run():
        class Controlled(Driver):
            def __init__(self):
                self.first_started = asyncio.Event()
                self.next_segment = asyncio.Event()
                self.contexts = []

            async def stream_segment(self, context, emit, tool):
                self.contexts.append(deepcopy(context))
                if context["segment_index"] == 0:
                    await emit("Первый.")
                    self.first_started.set()
                    await self.next_segment.wait()
                    return SegmentResult("Первый.", continue_response=True)
                sources = [source for result in context["background"]
                           for source in result["source_ids"]]
                await emit(" Второй.")
                return SegmentResult(" Второй.", used_source_ids=sources)

            async def run_background(self, agent, context, tool):
                return BackgroundResult(agent["agent_id"], source_ids=[f"kb:{agent['agent_id']}"])

        driver = Controlled()
        async with kernel(driver) as runtime:
            sid = (await runtime.create(CreateSession()))["session_id"]
            accepted = await runtime.submit(sid, turn())
            await completed(asyncio.create_task(driver.first_started.wait()))
            # next_step зависит от knowledge (граф агентов): ждём и задачи, созданные после
            for _ in range(3):
                await asyncio.gather(*(completed(t) for t in list(runtime.background_tasks[sid].values())))
            driver.next_segment.set()
            await completed(runtime.main_tasks[sid])
            state = await runtime.repo.get(sid)
            response = state["responses"][accepted["response_id"]]
            assert state["input_revision"] == 1
            assert len(state["background"]) == 2
            assert all(result["accepted"] for result in state["background"])
            assert len(driver.contexts[1]["background"]) == 2
            assert driver.contexts[1]["prefix"] == "Первый."
            assert response["status"] == "completed"
            assert len(response["segments"]) == 2
            assert set(response["segments"][1]["used_source_ids"]) == {
                "kb:knowledge", "kb:next_step",
            }
            events = await runtime.repo.events(sid)
            assert {e["response_id"] for e in events if e["type"] == "response.delta"} == {
                accepted["response_id"],
            }

    asyncio.run(run())


def test_interrupt_blocks_cancel_resistant_late_delta_but_preserves_background():
    async def run():
        class Controlled(Driver):
            def __init__(self):
                self.emitted = asyncio.Event()
                self.background_started = asyncio.Event()
                self.release_background = asyncio.Event()
                self.late_attempt = False

            async def stream_segment(self, context, emit, tool):
                await emit("Префикс.")
                self.emitted.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.late_attempt = True
                    await emit("LATE_MUST_NOT_ESCAPE")
                return SegmentResult("Префикс.")

            async def run_background(self, agent, context, tool):
                self.background_started.set()
                await self.release_background.wait()
                return BackgroundResult("still relevant", source_ids=["kb:claims.submission"])

        driver = Controlled()
        async with kernel(driver) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="research", instructions="Read KB"),
            ])))["session_id"]
            accepted = await runtime.submit(sid, turn())
            await completed(asyncio.create_task(driver.emitted.wait()))
            await completed(asyncio.create_task(driver.background_started.wait()))
            request = InterruptRequest(request_id=uuid4(), response_id=accepted["response_id"],
                                       played_ms=0)
            first = await runtime.playback(sid, request, interrupt=True)
            assert await runtime.playback(sid, request, interrupt=True) == first
            await completed(runtime.main_tasks[sid])
            assert driver.late_attempt
            assert not next(iter(runtime.background_tasks[sid].values())).done()
            driver.release_background.set()
            await asyncio.gather(*(completed(t) for t in runtime.background_tasks[sid].values()))
            state = await runtime.repo.get(sid)
            assert state["input_revision"] == 1
            assert package(state)["background"][0]["summary"] == "still relevant"
            response = state["responses"][accepted["response_id"]]
            assert response["status"] == "interrupted"
            assert response["segments"][0]["text"] == "Префикс."
            events = await runtime.repo.events(sid)
            stop_seq = next(e["seq"] for e in events if e["type"] == "response.interrupted")
            assert not [e for e in events if e["seq"] > stop_seq and e["type"] == "response.delta"]
            assert len([e for e in events if e["type"] == "response.interrupted"]) == 1

    asyncio.run(run())


def test_old_revision_background_cannot_enter_new_context():
    async def run():
        class Controlled(Driver):
            def __init__(self):
                self.old_started = asyncio.Event()
                self.release_old = asyncio.Event()
                self.contexts = []

            async def stream_segment(self, context, emit, tool):
                self.contexts.append(deepcopy(context))
                return await super().stream_segment(context, emit, tool)

            async def run_background(self, agent, context, tool):
                if context["input_revision"] == 1:
                    self.old_started.set()
                    try:
                        await self.release_old.wait()
                    except asyncio.CancelledError:
                        await self.release_old.wait()
                    return BackgroundResult("STALE_RESULT")
                return BackgroundResult("CURRENT_RESULT")

        driver = Controlled()
        async with kernel(driver) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="research", instructions="Read KB"),
            ])))["session_id"]
            await runtime.submit(sid, turn("Первый вопрос"))
            await completed(runtime.main_tasks[sid])
            await completed(asyncio.create_task(driver.old_started.wait()))
            old_task = next(iter(runtime.background_tasks[sid].values()))
            await runtime.submit(sid, turn("Новая тема"))
            await completed(runtime.main_tasks[sid])
            driver.release_old.set()
            await completed(old_task)
            await asyncio.gather(*(completed(t) for t in runtime.background_tasks[sid].values()))
            state = await runtime.repo.get(sid)
            assert state["input_revision"] == 2
            assert [result["summary"] for result in package(state)["background"]] == [
                "CURRENT_RESULT",
            ]
            assert all(result["summary"] != "STALE_RESULT"
                       for context in driver.contexts for result in context["background"])
            all_events = await runtime.repo.events(sid, public=False)
            assert any(e["type"] == "agent.stale" and e["payload"]["input_revision"] == 1
                       for e in all_events)

    asyncio.run(run())


def test_playback_delivery_timeline_monotonicity_and_interruption():
    async def run():
        class FourSegments(Driver):
            async def stream_segment(self, context, emit, tool):
                text = f"Сегмент {context['segment_index']}. "
                await emit(text)
                return SegmentResult(text, continue_response=context["segment_index"] < 3)

        async with kernel(FourSegments()) as runtime:
            sid = (await runtime.create(CreateSession(agents=[])))["session_id"]
            rid = (await runtime.submit(sid, turn()))["response_id"]
            await completed(runtime.main_tasks[sid])
            state = await runtime.repo.get(sid)
            segments = state["responses"][rid]["segments"]
            assert [s["delivery"]["status"] for s in history(state)[1]["segments"]] == [
                "unknown", "unknown", "unknown", "unknown",
            ]
            await runtime.playback(sid, PlaybackRequest(
                request_id=uuid4(), response_id=rid, played_ms=150,
                segments=[{"segment_id": segment["segment_id"], "start_ms": i * 100,
                           "end_ms": (i + 1) * 100} for i, segment in enumerate(segments[:3])],
            ))
            await runtime.playback(sid, PlaybackRequest(
                request_id=uuid4(), response_id=rid, played_ms=50,
            ))
            state = await runtime.repo.get(sid)
            delivered = history(state)[1]["segments"]
            assert [s["delivery"]["status"] for s in delivered] == [
                "heard", "partially_heard", "unheard", "unknown",
            ]
            assert delivered[1]["delivery"]["offset_ms"] == 50
            assert state["responses"][rid]["played_ms"] == 150
            await runtime.playback(sid, InterruptRequest(
                request_id=uuid4(), response_id=rid, played_ms=150,
            ), interrupt=True)
            with pytest.raises(KernelError, match="Нельзя продолжить"):
                await runtime.playback(sid, PlaybackRequest(
                    request_id=uuid4(), response_id=rid, played_ms=200,
                ))
            assert (await runtime.repo.get(sid))["responses"][rid]["status"] == "interrupted"

    asyncio.run(run())


def test_restart_closes_persisted_session_and_retains_partial_history():
    async def run():
        class Partial(Driver):
            def __init__(self):
                self.emitted = asyncio.Event()

            async def stream_segment(self, context, emit, tool):
                await emit("Сохранённый префикс.")
                self.emitted.set()
                await asyncio.Event().wait()

        driver = Partial()
        async with kernel(driver) as original:
            sid = (await original.create(CreateSession(agents=[])))["session_id"]
            rid = (await original.submit(sid, turn()))["response_id"]
            await completed(asyncio.create_task(driver.emitted.wait()))
            # Simulate process loss: cancel in-memory work without writing a shutdown event.
            original.main_tasks[sid].cancel()
            await completed(original.main_tasks[sid])
            recovered = Runtime(Driver(), repository=original.repo)
            await recovered.recover()
            await recovered.recover()
            state = await original.repo.get(sid)
            assert state["status"] == "closed"
            assert state["responses"][rid]["status"] == "interrupted"
            assert history(state)[1]["segments"][0]["text"] == "Сохранённый префикс."
            assert history(state)[1]["segments"][0]["delivery"]["status"] == "unknown"
            events = await original.repo.events(sid)
            closed = [e for e in events if e["type"] == "session.closed"]
            assert len(closed) == 1
            assert closed[0]["payload"]["reason"] == "server_restart"
            with pytest.raises(KernelError, match="Сессия завершена"):
                await recovered.submit(sid, turn())
            await recovered.shutdown()

    asyncio.run(run())


def test_cancelling_old_call_turn_does_not_stop_new_response():
    async def run():
        class Controlled(Driver):
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def stream_segment(self, context, emit, tool):
                if context["input_revision"] > 1:
                    self.started.set()
                    await self.release.wait()
                return await super().stream_segment(context, emit, tool)

        driver = Controlled()
        async with kernel(driver) as runtime:
            sid = (await runtime.create(CreateSession(agents=[])))["session_id"]
            first = await runtime.submit(sid, turn())
            await completed(runtime.main_tasks[sid])
            second = await runtime.submit(sid, turn())
            await driver.started.wait()
            await runtime.interrupt_turn(sid, first["turn_id"])
            assert not runtime.main_tasks[sid].cancelling()
            driver.release.set()
            await completed(runtime.main_tasks[sid])
            state = await runtime.repo.get(sid)
            assert state["responses"][second["response_id"]]["status"] == "completed"

    asyncio.run(run())
