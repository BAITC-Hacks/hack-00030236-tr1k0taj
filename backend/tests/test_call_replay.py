"""Idempotent voice/text ingress and persisted public replay with the real runtime."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from test_call_kernel import TTS, Executor, Router

from app.call import CallService, build_providers
from app.call.api import router as call_router
from app.config import settings
from app.context import Contexts, MemoryStore, PgStore
from app.kernel import KernelError, Repository, Runtime
from app.kernel.provider import BackgroundResult, SegmentResult
from app.knowledge import Knowledge, ensure_loaded
from app.speech import Transcript


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


class Driver:
    def __init__(self, blocked=False):
        self.contexts = []
        self.started, self.release = asyncio.Event(), asyncio.Event()
        if not blocked:
            self.release.set()

    async def stream_segment(self, context, emit, tool):
        self.contexts.append(context)
        await emit("Проверенный ответ.")
        self.started.set()
        if len(self.contexts) == 1:
            await self.release.wait()
        return SegmentResult("Проверенный ответ.")

    async def run_background(self, agent, context, tool):
        return BackgroundResult("PRIVATE_BACKGROUND_RESULT", source_ids=["kb:claims.submission"])

    async def close(self):
        pass


def application(calls):
    app = FastAPI()
    app.state.calls = calls
    app.include_router(call_router)

    @app.exception_handler(KernelError)
    async def error(_request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code}})

    return app


@asynccontextmanager
async def system(monkeypatch, driver, *, postgres=False):
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "embeddings_enabled", False)
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await ensure_loaded(db, Path(settings.datasets_dir))

    @asynccontextmanager
    async def knowledge():
        async with sessions() as db:
            yield Knowledge(db)

    monkeypatch.setattr("app.call.service.open_knowledge", knowledge)
    contexts = Contexts(PgStore(sessions) if postgres else MemoryStore())
    runtime = Runtime(driver, repository=Repository(contexts=contexts))
    counters = {"router": 0, "stt": 0, "tts": 0}

    class CountRouter(Router):
        async def route(self, *args):
            counters["router"] += 1
            return await super().route(*args)

    class CountTTS(TTS):
        async def synthesize(self, *args):
            counters["tts"] += 1
            return await super().synthesize(*args)

    class STT:
        name = "controlled"

        async def transcribe(self, *args):
            counters["stt"] += 1
            return Transcript(text="Статус заявления", language="ru")

    providers = build_providers(settings)
    providers.router, providers.executor = CountRouter(), Executor()
    providers.tts, providers.stt = CountTTS(), STT()
    calls = CallService(contexts, providers, runtime)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(application(calls)),
                                     base_url="http://test") as client:
            yield calls, runtime, client, counters, sessions
    finally:
        await calls.shutdown()
        await runtime.shutdown()
        await engine.dispose()


def test_text_duplicate_and_cursor_replay_do_not_repeat_router_or_tts(monkeypatch):
    async def run():
        driver = Driver()
        async with system(monkeypatch, driver) as (calls, _runtime, client, counts, _sessions):
            sid, rid = str(uuid4()), str(uuid4())
            path = f"/calls/{sid}/turns/text"
            payload = {"request_id": rid, "text": "Статус заявления"}
            first = await client.post(path, json=payload)
            assert first.status_code == 200 and first.headers["x-request-id"] == rid
            received = events(first)
            assert received[-1]["type"] == "turn.done"
            assert all(event["request_id"] == rid for event in received)
            assert [event["event_seq"] for event in received] == list(range(1, len(received) + 1))
            assert [event["seq"] for event in received if event["type"] == "audio"] == [0]
            before = counts.copy()
            second = await client.post(path, json=payload)
            assert events(second) == received and counts == before
            assert len(driver.contexts) == 1
            conflict = await client.post(path, json={**payload, "text": "Другой вопрос"})
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "request_conflict"
            cursor = received[3]["event_seq"]
            replay = await client.get(f"/calls/{sid}/events?request_id={rid}&after=1",
                                      headers={"Last-Event-ID": str(cursor)})
            assert events(replay) == [event for event in received if event["event_seq"] > cursor]
            assert counts == before and "PRIVATE_BACKGROUND_RESULT" not in replay.text
            invalid = await client.get(f"/calls/{sid}/events?after=9999")
            assert invalid.status_code == 422
            assert "call_journal" not in (await calls.contexts.snapshot(sid)).model_dump()
            # A reset hides the old generation and cannot re-execute its request identifier.
            await calls.contexts.start_call(sid)
            assert events(await client.get(f"/calls/{sid}/events")) == []
            assert (await client.post(path, json=payload)).status_code == 409

    asyncio.run(run())


def test_duplicate_while_active_joins_same_stream_and_new_request_needs_interrupt(monkeypatch):
    async def run():
        driver = Driver(blocked=True)
        async with system(monkeypatch, driver) as (_calls, _runtime, client, counts, _sessions):
            sid, rid = str(uuid4()), str(uuid4())
            path = f"/calls/{sid}/turns/text"
            payload = {"request_id": rid, "text": "Статус заявления"}
            first = asyncio.create_task(client.post(path, json=payload))
            await asyncio.wait_for(driver.started.wait(), 5)
            duplicate = asyncio.create_task(client.post(path, json=payload))
            conflict = await client.post(path, json={**payload, "request_id": str(uuid4())})
            assert conflict.status_code == 409
            driver.release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, duplicate), 5)
            assert events(responses[0]) == events(responses[1])
            assert counts["router"] == counts["tts"] == 1

    asyncio.run(run())


def test_audio_retries_use_hash_and_never_store_input_bytes(monkeypatch):
    async def run():
        async with system(monkeypatch, Driver()) as (calls, _runtime, client, counts, _sessions):
            sid, rid = str(uuid4()), str(uuid4())
            path = f"/calls/{sid}/turns/audio"
            audio = b"PRIVATE_INPUT_AUDIO_BYTES"
            first = await client.post(path, data={"request_id": rid, "language_hint": "ru"},
                                      files={"audio": ("input.webm", audio, "audio/webm")})
            second = await client.post(path, data={"request_id": rid, "language_hint": "ru"},
                                       files={"audio": ("renamed.webm", audio, "audio/webm")})
            assert first.status_code == second.status_code == 200
            assert events(first) == events(second)
            assert counts == {"router": 1, "stt": 1, "tts": 1}
            conflict = await client.post(path, data={"request_id": rid, "language_hint": "ru"},
                                         files={"audio": ("input.webm", b"different", "audio/webm")})
            assert conflict.status_code == 409
            board = [entry.model_dump() for entry in await calls.contexts.board(sid)]
            assert "PRIVATE_INPUT_AUDIO_BYTES" not in json.dumps(board)
            replay = await client.get(f"/calls/{sid}/events?request_id={rid}")
            assert events(replay) == events(first)

    asyncio.run(run())


def test_interrupt_previous_persists_old_terminal_and_forwards_task_updates(monkeypatch):
    async def run():
        driver = Driver(blocked=True)
        async with system(monkeypatch, driver) as (calls, runtime, client, _counts, _sessions):
            sid, old_id = str(uuid4()), str(uuid4())
            path = f"/calls/{sid}/turns/text"
            first = asyncio.create_task(client.post(path, json={
                "request_id": old_id, "text": "Статус заявления",
            }))
            await asyncio.wait_for(driver.started.wait(), 5)
            second = await asyncio.wait_for(client.post(path, json={
                "request_id": str(uuid4()), "text": "Поправка", "task_id": "claim-b",
                "interrupt_previous": True,
                "updates": [{"key": "amount", "value": 150}],
            }), 5)
            old = await asyncio.wait_for(first, 5)
            assert events(old)[-1]["type"] == "turn.cancelled"
            assert events(second)[-1]["type"] == "turn.done"
            assert len(driver.contexts) == 2
            assert driver.contexts[-1]["task_id"] == "claim-b"
            assert (await calls.contexts.snapshot(sid)).turn_id == 2
            state = await runtime.repo.get(sid)
            amount = next(record for record in state["blackboard"]["records"].values()
                          if record["key"] == "amount")
            assert amount["task_id"] == "claim-b" and amount["value"] == 150
            response = next(r for r in state["responses"].values()
                            if r["turn_id"] == 1)
            assert response["status"] == "interrupted" and response["played_ms"] is None
            assert events(await client.get(f"/calls/{sid}/events?request_id={old_id}")) == events(old)

    asyncio.run(run())


def test_template_turn_still_applies_explicit_task_inputs(monkeypatch):
    async def run():
        driver = Driver()
        async with system(monkeypatch, driver) as (calls, runtime, client, _counts, _sessions):
            calls.providers.router = build_providers(settings).router
            sid = str(uuid4())
            response = await client.post(f"/calls/{sid}/turns/text", json={
                "request_id": str(uuid4()), "text": "Поправка", "task_id": "claim-a",
                "updates": [{"key": "amount", "value": 150}],
            })
            assert events(response)[-1]["type"] == "turn.done"
            assert driver.contexts == []
            state = await runtime.repo.get(sid)
            assert state["turn_tasks"]["1"] == "claim-a"
            amount = next(record for record in state["blackboard"]["records"].values()
                          if record["key"] == "amount")
            assert amount["task_id"] == "claim-a" and amount["value"] == 150

    asyncio.run(run())


def test_postgres_replay_after_owner_reload_and_orphan_recovery(monkeypatch):
    async def run():
        async with system(monkeypatch, Driver(), postgres=True) as (calls, runtime, client, counts, sf):
            sid, rid = str(uuid4()), str(uuid4())
            payload = {"request_id": rid, "text": "Статус заявления"}
            original = await client.post(f"/calls/{sid}/turns/text", json=payload)
            expected = events(original)
            orphan = str(uuid4())
            await calls.journal.begin(sid, orphan, "synthetic-crashed-request")
            await asyncio.gather(*runtime.background_tasks[sid].values())
            reloaded_contexts = Contexts(PgStore(sf))
            fresh_driver = Driver()
            fresh_runtime = Runtime(fresh_driver, repository=Repository(contexts=reloaded_contexts))
            restored = CallService(reloaded_contexts, calls.providers, fresh_runtime)
            before = counts.copy()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(application(restored)),
                                             base_url="http://test") as fresh:
                    replay = await fresh.get(f"/calls/{sid}/events?request_id={rid}")
                    assert events(replay) == expected
                    repeated = await fresh.post(f"/calls/{sid}/turns/text", json=payload)
                    assert events(repeated) == expected
                    assert counts == before and fresh_driver.contexts == []
                    recovered = await fresh.get(f"/calls/{sid}/events?request_id={orphan}")
                    assert events(recovered)[-1]["code"] == "request_interrupted"
            finally:
                await restored.shutdown()
                await fresh_runtime.shutdown()

    asyncio.run(run())
