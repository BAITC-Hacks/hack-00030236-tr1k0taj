"""Call protocol → real kernel runtime, shared context, and controlled model streams."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.call import CallService, build_providers
from app.call.api import router as call_router
from app.call.ports import AudioChunk, Execution, ReplyBrief
from app.config import settings
from app.context import Contexts, Fact, MemoryStore
from app.kernel import Repository, Runtime
from app.kernel.context import history
from app.kernel.provider import BackgroundResult, SegmentResult
from app.knowledge import Knowledge, ensure_loaded
from app.router import RouterOutput, RouterResult


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "embeddings_enabled", False)


class Router:
    name = "controlled"

    async def route(self, utterance, view, kb):
        assert len(await kb.catalog.scenario_ids()) == 43
        return RouterResult(output=RouterOutput.model_validate({
            "scenarios": [{"scenario_id": "SC17", "confidence": 0.9, "reason": "status"}],
            "language": "ru", "slots": {},
        }), model="controlled", prompt="private router prompt", raw="private raw")


class Executor:
    name = "controlled"
    supported_actions: ClassVar[list[str]] = []

    async def execute(self, turn):
        fact = Fact(key="claim.status", value="documents_requested", source="get_claim",
                    source_id="CL-500311")
        return Execution(facts=[fact], brief=ReplyBrief(
            language="ru", scenario_id="SC17", decision="route", instruction="Сообщи статус",
            facts=[fact],
        ))


class TTS:
    name = "controlled"

    async def synthesize(self, text, language):
        return AudioChunk(mime="audio/wav", data=b"controlled audio")


class Driver:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.background_release = asyncio.Event()
        self.background_release.set()
        self.contexts = []

    async def stream_segment(self, context, emit, tool):
        self.contexts.append(context)
        if context["segment_index"] == 0:
            await emit("Статус проверен.")
            self.started.set()
            await self.release.wait()
            return SegmentResult("Статус проверен.", continue_response=True)
        text = f" Источников фона: {len(context['background'])}."
        await emit(text)
        return SegmentResult(text)

    async def run_background(self, agent, context, tool):
        await self.background_release.wait()
        return BackgroundResult("INTERNAL_RESEARCH", source_ids=["kb:claims.submission"])

    async def close(self):
        pass


@asynccontextmanager
async def system(monkeypatch, driver, *, routed=True):
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await ensure_loaded(db, Path(settings.datasets_dir))

    @asynccontextmanager
    async def knowledge():
        async with sessions() as db:
            yield Knowledge(db)

    monkeypatch.setattr("app.call.service.open_knowledge", knowledge)
    contexts = Contexts(MemoryStore())
    runtime = Runtime(driver, repository=Repository(contexts=contexts))
    providers = build_providers(settings)
    if routed:
        providers.router, providers.executor, providers.tts = Router(), Executor(), TTS()
    calls = CallService(contexts, providers, runtime)
    app = FastAPI()
    app.state.calls = calls
    app.include_router(call_router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://test") as client:
            yield calls, runtime, client
    finally:
        await runtime.shutdown()
        await engine.dispose()


def events(text):
    return [json.loads(line.removeprefix("data: ")) for line in text.splitlines()
            if line.startswith("data: ")]


def test_call_sse_preserves_frontend_uuid_and_turn_with_live_kernel_background(monkeypatch):
    async def run():
        driver = Driver()
        async with system(monkeypatch, driver) as (calls, runtime, client):
            sid = str(uuid4())
            opened = await client.post("/calls", json={"session_id": sid})
            assert opened.json()["session_id"] == sid
            assert opened.json()["capabilities"]["kernel_enabled"]
            request = asyncio.create_task(client.post(f"/calls/{sid}/turns/text",
                                                       json={"text": "Статус заявления"}))
            await asyncio.wait_for(driver.started.wait(), 5)
            await asyncio.gather(*runtime.background_tasks[sid].values())
            driver.release.set()
            response = await asyncio.wait_for(request, 5)
            received = events(response.text)
            assert [event["type"] for event in received[:3]] == [
                "transcript", "turn.started", "routing",
            ]
            assert received[1]["session_id"] == sid
            assert all(event["turn_id"] == 1 for event in received)
            assert received[-1]["type"] == "turn.done"
            assert "Источников фона: 2" in received[-1]["reply"]
            assert "INTERNAL_RESEARCH" not in response.text
            context = await calls.contexts.snapshot(sid)
            assert context.turn_id == 1
            assert len([item for item in context.history if item.role == "client"]) == 1
            assert driver.contexts[0]["context"]["call_brief"]["facts"][0]["source_id"] == "CL-500311"
            deltas = [item for item in received if item["type"] == "reply.delta"]
            audio = [item for item in received if item["type"] == "audio"]
            rid = deltas[0]["response_id"]
            assert rid and {item["response_id"] for item in deltas + audio} == {rid}
            # TTS по предложениям: на сегмент одно или несколько аудио
            segments = list(dict.fromkeys(item["segment_id"] for item in audio))
            assert len(segments) == 2
            assert set(segments) == {
                item["segment_id"] for item in deltas
            }
            ack = await client.post(f"/calls/{sid}/turns/1/playback", json={
                "response_id": rid, "played_ms": 150, "segments": [
                    {"segment_id": segment_id, "start_ms": i * 100,
                     "end_ms": (i + 1) * 100} for i, segment_id in enumerate(segments)
                ],
            })
            assert ack.status_code == 204
            assert [item["delivery"]["status"] for item in history(await runtime.repo.get(sid))[1]["segments"]] == [
                "heard", "partially_heard",
            ]
            legacy = await client.post(f"/calls/{sid}/turns/1/playback",
                                       json={"eos_to_playback_ms": 1234})
            assert legacy.status_code == 204

    asyncio.run(run())


def test_call_cancel_without_position_preserves_unknown_and_background(monkeypatch):
    async def run():
        driver = Driver()
        driver.background_release.clear()
        async with system(monkeypatch, driver) as (calls, runtime, client):
            sid = str(uuid4())
            await calls.ensure_call(sid)
            received, delta = [], asyncio.Event()

            async def collect():
                async for item in calls.run_turn(sid, text="Статус заявления"):
                    received.append(item)
                    if item.type == "reply.delta":
                        delta.set()

            response = asyncio.create_task(collect())
            await asyncio.wait_for(delta.wait(), 5)
            stop = await client.post(f"/calls/{sid}/turns/1/cancel")
            assert stop.status_code == 204
            await asyncio.wait_for(response, 5)
            assert received[-1].type == "turn.cancelled"
            assert not any(item.type == "turn.done" for item in received)
            state = await runtime.repo.get(sid)
            assert history(state)[1]["segments"][0]["delivery"]["status"] == "unknown"
            assert all(not task.done() for task in runtime.background_tasks[sid].values())
            driver.background_release.set()
            await asyncio.gather(*runtime.background_tasks[sid].values())
            assert len((await runtime.repo.get(sid))["background"]) == 2

    asyncio.run(run())


def test_call_mock_router_preserves_honest_template_without_kernel_generation(monkeypatch):
    async def run():
        driver = Driver()
        async with system(monkeypatch, driver, routed=False) as (_calls, _runtime, client):
            sid = str(uuid4())
            response = await client.post(f"/calls/{sid}/turns/text", json={"text": "Вопрос"})
            received = events(response.text)
            assert any(item["type"] == "error" and item["code"] == "llm_unavailable"
                       for item in received)
            assert received[-1]["type"] == "turn.done"
            assert driver.contexts == []

    asyncio.run(run())
