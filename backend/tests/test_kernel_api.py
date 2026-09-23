"""HTTP contracts and finite SSE replay against the real journal."""

import asyncio
import json
from itertools import pairwise
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_kernel_runtime import Driver, completed, kernel

from app.config import settings
from app.kernel.api import router
from app.kernel.provider import BackgroundResult, SegmentResult
from app.kernel.store import KernelError
from app.main import kernel_error


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "embeddings_enabled", False)


def client_for(runtime):
    app = FastAPI()
    app.state.kernel = runtime
    app.include_router(router)
    app.add_exception_handler(KernelError, kernel_error)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_turn_http_idempotency_payload_conflict_and_active_response():
    async def run():
        class Controlled(Driver):
            def __init__(self):
                self.release = asyncio.Event()

            async def stream_segment(self, context, emit, tool):
                await self.release.wait()
                return await super().stream_segment(context, emit, tool)

        driver = Controlled()
        async with kernel(driver) as runtime, client_for(runtime) as client:
            created = await client.post("/sessions", json={"agents": []})
            assert created.status_code == 201
            sid = created.json()["session_id"]
            path = f"/sessions/{sid}/turns"
            payload = {"request_id": str(uuid4()), "text": "Что с заявлением?"}
            first = await client.post(path, json=payload)
            duplicate = await client.post(path, json=payload)
            assert first.status_code == duplicate.status_code == 202
            assert first.json() == duplicate.json()
            conflict = await client.post(path, json={**payload, "text": "Другой вопрос"})
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "request_conflict"
            busy = await client.post(path, json={**payload, "request_id": str(uuid4())})
            assert busy.status_code == 409
            assert busy.json()["error"]["code"] == "response_active"
            invalid = await client.post(path, json={**payload, "extra_field": "rejected"})
            assert invalid.status_code == 422
            driver.release.set()
            await completed(runtime.main_tasks[sid])
            replay = await client.post(path, json=payload)
            assert replay.json() == first.json()
            state = await runtime.repo.get(sid)
            assert len(state["messages"]) == 1
            events = await runtime.repo.events(sid)
            assert len([e for e in events if e["type"] == "response.started"]) == 1

    asyncio.run(run())


def test_closed_sse_replay_filters_internal_events_and_honors_cursor():
    async def run():
        class PrivateDriver(Driver):
            async def stream_segment(self, context, emit, tool):
                await emit("Публичный ответ.")
                return SegmentResult("Публичный ответ.", used_source_ids=["kb:claims.submission"])

            async def run_background(self, agent, context, tool):
                await tool("rag_read", {"kind": "kb", "key": "claims.submission"})
                return BackgroundResult("INTERNAL_RESULT_MUST_NOT_ESCAPE",
                                        source_ids=["kb:claims.submission"])

        async def private_tool(name, args):
            return {"content": "INTERNAL_TOOL_MUST_NOT_ESCAPE", "source_id": "kb:claims.submission"}

        async with kernel(PrivateDriver()) as runtime, client_for(runtime) as client:
            runtime.tool_executor = private_tool
            created = await client.post("/sessions", json={"agents": [{
                "agent_id": "research", "instructions": "INTERNAL_PROMPT_MUST_NOT_ESCAPE",
            }]})
            sid = created.json()["session_id"]
            await client.post(f"/sessions/{sid}/turns", json={
                "request_id": str(uuid4()), "text": "Куда подать документы?",
            })
            await completed(runtime.main_tasks[sid])
            await asyncio.gather(*(completed(t) for t in runtime.background_tasks[sid].values()))
            internal = await runtime.repo.events(sid, public=False)
            assert any(e["type"] == "agent.result" for e in internal)
            assert any(e["type"] == "tool.completed" for e in internal)
            assert (await client.post(f"/sessions/{sid}/close")).status_code == 200
            assert (await client.post(f"/sessions/{sid}/close")).status_code == 200
            response = await asyncio.wait_for(client.get(f"/sessions/{sid}/events"), timeout=5)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            items = [json.loads(line[6:]) for line in response.text.splitlines()
                     if line.startswith("data: ")]
            public = await runtime.repo.events(sid)
            assert items == public
            assert items[-1]["type"] == "session.closed"
            assert len([e for e in items if e["type"] == "session.closed"]) == 1
            assert all(e["visibility"] == "public" and not e["type"].startswith(("agent.", "tool."))
                       for e in items)
            seqs = [e["seq"] for e in items]
            assert seqs == sorted(set(seqs))
            assert any(right - left > 1 for left, right in pairwise(seqs))
            for suffix in ("", "/trace", "/history"):
                projection = await client.get(f"/sessions/{sid}{suffix}")
                assert projection.status_code == 200
                assert "INTERNAL_" not in projection.text
            assert "INTERNAL_" not in response.text
            resumed = await client.get(f"/sessions/{sid}/events?after={seqs[0]}",
                                       headers={"Last-Event-ID": str(seqs[2])})
            resumed_items = [json.loads(line[6:]) for line in resumed.text.splitlines()
                             if line.startswith("data: ")]
            assert resumed_items == [item for item in items if item["seq"] > seqs[2]]
            empty = await client.get(f"/sessions/{sid}/events?after={seqs[-1]}")
            assert empty.text == ""
            bad_cursor = await client.get(f"/sessions/{sid}/events?after={seqs[-1] + 1}")
            assert bad_cursor.status_code == 422

    asyncio.run(run())


def test_live_mode_without_key_returns_explicit_error_without_running_driver(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)

    async def run():
        class NeverCalled(Driver):
            async def stream_segment(self, context, emit, tool):
                raise AssertionError("Missing credentials must be rejected before generation")

        async with kernel(NeverCalled()) as runtime, client_for(runtime) as client:
            capabilities = (await client.get("/kernel/capabilities")).json()
            assert capabilities["mode"] == "live" and not capabilities["llm_configured"]
            sid = (await client.post("/sessions", json={"agents": []})).json()["session_id"]
            response = await client.post(f"/sessions/{sid}/turns", json={
                "request_id": str(uuid4()), "text": "Вопрос о полисе",
            })
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "provider_not_configured"
            assert runtime.main_tasks == {}
            assert (await runtime.repo.get(sid))["messages"] == []

    asyncio.run(run())


def test_close_between_empty_event_read_and_state_read_is_delivered():
    async def run():
        async with kernel(Driver()) as runtime, client_for(runtime) as client:
            sid = (await client.post("/sessions", json={"agents": []})).json()["session_id"]
            original = runtime.repo.events
            reads = 0

            async def racing_events(session_id, after=0, **kwargs):
                nonlocal reads
                reads += 1
                if reads == 1:
                    await runtime.close_session(sid)
                    return []  # query snapshot preceded the close commit
                return await original(session_id, after, **kwargs)

            runtime.repo.events = racing_events
            result = await asyncio.wait_for(client.get(f"/sessions/{sid}/events?after=1"), 5)
            assert "event: session.closed" in result.text
            assert reads >= 2

    asyncio.run(run())


def test_playback_cannot_acknowledge_future_text_of_generating_segment():
    async def run():
        class Waiting(Driver):
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def stream_segment(self, context, emit, tool):
                await emit("Начало.")
                self.started.set()
                await self.release.wait()
                await emit(" Конец.")
                return SegmentResult("Начало. Конец.")

        driver = Waiting()
        async with kernel(driver) as runtime, client_for(runtime) as client:
            sid = (await client.post("/sessions", json={"agents": []})).json()["session_id"]
            accepted = (await client.post(f"/sessions/{sid}/turns", json={
                "request_id": str(uuid4()), "text": "Расскажите про полис",
            })).json()
            await completed(asyncio.create_task(driver.started.wait()))
            response = (await client.get(f"/sessions/{sid}/history")).json()["messages"][-1]
            segment = response["segments"][0]["segment_id"]
            result = await client.post(f"/sessions/{sid}/playback", json={
                "request_id": str(uuid4()), "response_id": accepted["response_id"],
                "played_ms": 0, "text_segment_ids": [segment],
            })
            assert result.status_code == 409
            driver.release.set()
            await completed(runtime.main_tasks[sid])
            response = (await client.get(f"/sessions/{sid}/history")).json()["messages"][-1]
            assert response["segments"][0]["delivery"]["status"] == "unknown"

    asyncio.run(run())
