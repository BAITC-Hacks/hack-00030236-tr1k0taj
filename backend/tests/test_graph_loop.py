"""Background agent graph: retries, blocked dependents, cross-turn tool cache,
between-turn scheduling, record-driven reschedule, budgets, blocking deadlines.
No LLM, no Docker: an in-memory context store stands in for Postgres.
"""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from app.config import settings
from app.context import Contexts, MemoryStore
from app.kernel.provider import BackgroundResult, SegmentResult
from app.kernel.runtime import Runtime
from app.kernel.store import Repository
from app.kernel.types import AgentSpec, CreateSession, RecordRequest, TurnRequest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "kernel_max_segments", 4)


@asynccontextmanager
async def kernel(driver, tool_executor=None):
    repo = Repository(contexts=Contexts(MemoryStore()))
    runtime = Runtime(driver, repository=repo, tool_executor=tool_executor)
    try:
        yield runtime
    finally:
        await runtime.shutdown()


class Driver:
    async def stream_segment(self, context, emit, tool):
        await emit("Ответ.")
        return SegmentResult("Ответ.")

    async def run_background(self, agent, context, tool):
        return BackgroundResult("ok", source_ids=["kb:x"])

    async def close(self):
        pass


def turn(text="Вопрос?"):
    return TurnRequest(request_id=uuid4(), text=text)


async def completed(task):
    return await asyncio.wait_for(asyncio.shield(task), timeout=5)


async def drain(runtime, sid):
    """Await every currently pending background task until the run count settles."""
    previous = -1
    for _ in range(100):
        pending = [t for t in runtime.background_tasks[sid].values() if not t.done()]
        if pending:
            await asyncio.gather(*pending)
            continue
        state = await runtime.repo.get(sid)
        if len(state["runs"]) == previous:
            return state
        previous = len(state["runs"])
    raise AssertionError("background graph did not settle")


def test_failed_agent_gets_exactly_one_retry():
    async def run():
        class Flaky(Driver):
            def __init__(self):
                self.attempts = 0

            async def run_background(self, agent, context, tool):
                self.attempts += 1
                raise RuntimeError("boom")

        driver = Flaky()
        async with kernel(driver) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="research", instructions="Look things up"),
            ])))["session_id"]
            await runtime.submit(sid, turn())
            await completed(runtime.main_tasks[sid])
            state = await drain(runtime, sid)
            assert driver.attempts == 2
            statuses = sorted(r["status"] for r in state["runs"].values())
            assert statuses == ["failed", "failed"]
            retried = [r for r in state["runs"].values() if r.get("retry_of")]
            assert len(retried) == 1

    asyncio.run(run())


def test_dependent_becomes_blocked_after_parent_exhausts_retry():
    async def run():
        class AlwaysFails(Driver):
            async def run_background(self, agent, context, tool):
                if agent["agent_id"] == "knowledge":
                    raise RuntimeError("boom")
                return BackgroundResult("next step", source_ids=["kb:step"])

        async with kernel(AlwaysFails()) as runtime:
            sid = (await runtime.create(CreateSession()))["session_id"]
            await runtime.submit(sid, turn())
            await completed(runtime.main_tasks[sid])
            state = await drain(runtime, sid)
            next_step_runs = [r for r in state["runs"].values() if r["agent_id"] == "next_step"]
            assert len(next_step_runs) == 1
            assert next_step_runs[0]["status"] == "blocked"
            assert next_step_runs[0]["task_id"] == "default"
            events = await runtime.repo.events(sid, public=False)
            blocked = [e for e in events if e["type"] == "agent.blocked"]
            assert len(blocked) == 1
            assert blocked[0]["payload"]["blocked_by"] == ["knowledge"]

    asyncio.run(run())


def test_tool_cache_survives_across_turns():
    async def run():
        calls = []

        async def tool_executor(name, args):
            calls.append((name, args["query"]))
            return {"hits": [{"source_id": "kb:claims", "content": "text"}]}

        class Reader(Driver):
            async def run_background(self, agent, context, tool):
                result = await tool("rag_search", {
                    "query": "claims", "kinds": ["kb"], "limit": 3,
                })
                return BackgroundResult("ok", source_ids=[h["source_id"] for h in result["hits"]])

        async with kernel(Reader(), tool_executor=tool_executor) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="research", instructions="Look up claims"),
            ])))["session_id"]
            await runtime.submit(sid, turn("Первый вопрос"))
            await completed(runtime.main_tasks[sid])
            await drain(runtime, sid)
            await runtime.submit(sid, turn("Второй вопрос"))
            await completed(runtime.main_tasks[sid])
            await drain(runtime, sid)
            assert calls == [("rag_search", "claims")]

    asyncio.run(run())


def test_next_step_waits_for_knowledge_instead_of_searching_in_parallel():
    async def run():
        started = []

        class Ordered(Driver):
            async def run_background(self, agent, context, tool):
                started.append(agent["agent_id"])
                return BackgroundResult(agent["agent_id"], source_ids=[f"kb:{agent['agent_id']}"])

        async with kernel(Ordered()) as runtime:
            sid = (await runtime.create(CreateSession()))["session_id"]
            await runtime.submit(sid, turn())
            await completed(runtime.main_tasks[sid])
            await drain(runtime, sid)
            assert started == ["knowledge", "next_step"]

    asyncio.run(run())


def test_agent_on_response_completed_runs_after_response_with_no_public_events():
    async def run():
        async with kernel(Driver()) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="followup", instructions="Keep reading",
                          on=["response.completed"]),
            ])))["session_id"]
            await runtime.submit(sid, turn())
            await completed(runtime.main_tasks[sid])
            after_response = await runtime.repo.events(sid)
            assert after_response[-1]["type"] == "response.completed"
            state = await drain(runtime, sid)
            runs = [r for r in state["runs"].values() if r["agent_id"] == "followup"]
            assert len(runs) == 1 and runs[0]["status"] == "completed"
            after_background = await runtime.repo.events(sid)
            assert after_background == after_response

    asyncio.run(run())


def test_record_change_reschedules_only_the_agent_reading_that_key():
    async def run():
        started = []

        class Watchers(Driver):
            async def run_background(self, agent, context, tool):
                started.append(agent["agent_id"])
                return BackgroundResult(agent["agent_id"])

        async with kernel(Watchers()) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="watch_x", instructions="Watch x", on=["record.changed"],
                          reads=["x"]),
                AgentSpec(agent_id="watch_y", instructions="Watch y", on=["record.changed"],
                          reads=["y"]),
            ])))["session_id"]
            await runtime.update_record(sid, RecordRequest(
                request_id=uuid4(), key="x", value="v1", task_id="default",
            ))
            await drain(runtime, sid)
            assert started == ["watch_x"]

    asyncio.run(run())


def test_budget_stops_a_self_feeding_graph(monkeypatch):
    monkeypatch.setattr(settings, "kernel_max_runs_per_turn", 4)
    monkeypatch.setattr(settings, "kernel_max_runs_per_session", 4)

    async def run():
        class PingPong(Driver):
            async def run_background(self, agent, context, tool):
                return BackgroundResult(agent["agent_id"])

        async with kernel(PingPong()) as runtime:
            # Two agents cross-reading each other's output retrigger one another forever;
            # only the hard budget below can stop this self-feeding graph.
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="ping", instructions="Ping", reads=["agent:pong"],
                          on=["user.message", "agent.result"]),
                AgentSpec(agent_id="pong", instructions="Pong", reads=["agent:ping"],
                          on=["user.message", "agent.result"]),
            ])))["session_id"]
            await runtime.submit(sid, turn())
            await completed(runtime.main_tasks[sid])
            state = await drain(runtime, sid)
            assert len(state["runs"]) == 4
            events = await runtime.repo.events(sid, public=False)
            assert any(e["type"] == "graph.budget_exhausted" for e in events)

    asyncio.run(run())


def test_blocking_agent_does_not_delay_the_response_past_its_deadline():
    async def run():
        class SlowAgent(Driver):
            async def run_background(self, agent, context, tool):
                await asyncio.sleep(2)
                return BackgroundResult("too late")

        async with kernel(SlowAgent()) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="slow", instructions="Take a while",
                          blocking=True, deadline_ms=100),
            ])))["session_id"]
            rid = (await runtime.submit(sid, turn()))["response_id"]
            await asyncio.wait_for(asyncio.shield(runtime.main_tasks[sid]), timeout=1)
            state = await runtime.repo.get(sid)
            assert state["responses"][rid]["status"] == "completed"
            assert state["responses"][rid]["blocking_incomplete"] == ["slow"]
            await drain(runtime, sid)

    asyncio.run(run())
