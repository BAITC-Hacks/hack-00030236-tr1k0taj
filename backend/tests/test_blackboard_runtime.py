"""Persisted dependency scheduling and atomic interruption, with controlled providers."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from test_kernel_runtime import Driver, completed, kernel

from app.context import ensure_blackboard, select_records
from app.kernel.context import package
from app.kernel.provider import BackgroundResult, SegmentResult
from app.kernel.store import KernelError
from app.kernel.types import (
    AgentSpec,
    BackgroundCancelRequest,
    CreateSession,
    RecordRequest,
    TaskRequest,
    TurnRequest,
)


def test_independent_run_survives_new_message_and_correction_revokes_its_result():
    async def run():
        class Controlled(Driver):
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.contexts = []

            async def run_background(self, agent, context, tool):
                self.contexts.append(deepcopy(context))
                self.started.set()
                await self.release.wait()
                allowed = await tool("blackboard_read", {"keys": ["city"]})
                forbidden = await tool("blackboard_read", {"keys": ["policy"]})
                assert [record["key"] for record in allowed["records"]] == ["city"]
                assert forbidden == {"error": "undeclared_record_read"}
                return BackgroundResult("Office in Almaty")

        driver = Controlled()
        async with kernel(driver) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="office", instructions="Find office", reads=["city"]),
            ])))["session_id"]
            await runtime.submit(sid, TurnRequest(request_id=uuid4(), text="Где офис?", updates=[
                {"key": "city", "value": "Almaty"}, {"key": "policy", "value": "PRIVATE"},
            ]))
            await completed(runtime.main_tasks[sid])
            await completed(asyncio.create_task(driver.started.wait()))
            original = next(iter(runtime.background_tasks[sid].values()))
            await runtime.submit(sid, TurnRequest(request_id=uuid4(), text="Расскажите об оплате"))
            await completed(runtime.main_tasks[sid])
            assert not original.cancelling()
            assert len(runtime.background_tasks[sid]) == 1
            driver.release.set()
            await completed(original)
            state = await runtime.repo.get(sid)
            assert package(state)["background"][0]["summary"] == "Office in Almaty"
            assert driver.contexts[0]["user_text"] == ""
            assert driver.contexts[0]["history"] == []
            assert "PRIVATE" not in str(driver.contexts)
            update = RecordRequest(request_id=uuid4(), key="city", value="Astana", task_id="default")
            first = await runtime.update_record(sid, update)
            assert await runtime.update_record(sid, update) == first
            state = await runtime.repo.get(sid)
            assert package(state)["background"] == []
            assert [record["value"] for record in select_records(state, "default", ["city"])] == ["Astana"]
            assert "Office in Almaty" not in str(await runtime.list_records(sid))
            assert len(state["responses"]) == 2  # Correction never starts unsolicited speech.
            with pytest.raises(KernelError, match="request_id"):
                await runtime.update_record(sid, update.model_copy(update={"value": "Shymkent"}))

    asyncio.run(run())


def test_missing_input_then_appearance_invalidates_completed_output():
    async def run():
        async with kernel(Driver()) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="research", instructions="Inspect optional city", reads=["city"]),
            ])))["session_id"]
            await runtime.submit(sid, TurnRequest(request_id=uuid4(), text="Условия страхования"))
            await completed(runtime.main_tasks[sid])
            await asyncio.gather(*(completed(task) for task in runtime.background_tasks[sid].values()))
            state = await runtime.repo.get(sid)
            result_id = state["background"][0]["record_id"]
            assert any(record["record_id"] == result_id for record in select_records(state, "default"))
            await runtime.update_record(sid, RecordRequest(
                request_id=uuid4(), task_id="default", key="city", value="Almaty",
            ))
            state = await runtime.repo.get(sid)
            assert ensure_blackboard(state)["records"][result_id]["status"] == "stale"
            assert package(state)["background"] == []

    asyncio.run(run())


def test_dag_child_reads_parent_record_and_parent_change_revokes_child():
    async def run():
        class Controlled(Driver):
            def __init__(self):
                self.children = []

            async def run_background(self, agent, context, tool):
                if agent["agent_id"] == "child":
                    self.children.append(deepcopy(context))
                    parent = next(record for record in context["blackboard"]
                                  if record["key"] == "agent:parent")
                    assert parent["value"]["summary"] == "Verified parent result"
                    return BackgroundResult("Child based on parent")
                return BackgroundResult("Verified parent result")

        driver = Controlled()
        async with kernel(driver) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="parent", instructions="Find office", reads=["city"]),
                AgentSpec(agent_id="child", instructions="Check parent office", reads=["policy"],
                          depends_on=["parent"], on=["agent.result"]),
            ])))["session_id"]
            await runtime.submit(sid, TurnRequest(request_id=uuid4(), text="Адрес офиса", updates=[
                {"key": "city", "value": "Almaty"}, {"key": "policy", "value": "Insurance"},
            ]))
            await completed(runtime.main_tasks[sid])
            # Completing a parent may add its child after this snapshot of task references.
            for _ in range(2):
                await asyncio.gather(*(completed(task) for task in list(runtime.background_tasks[sid].values())))
            state = await runtime.repo.get(sid)
            child = next(item for item in state["background"] if item["agent_id"] == "child")
            parent = next(item for item in state["background"] if item["agent_id"] == "parent")
            assert child["read_versions"]["agent:parent"] == parent["record_id"]
            assert driver.children[0]["user_text"] == "" and driver.children[0]["history"] == []
            await runtime.update_record(sid, RecordRequest(
                request_id=uuid4(), task_id="default", key="city", value="Astana",
            ))
            state = await runtime.repo.get(sid)
            assert package(state)["background"] == []
            board = ensure_blackboard(state)
            assert board["records"][child["record_id"]]["status"] == "stale"

    asyncio.run(run())


def test_pause_and_background_cancel_are_scoped_and_reject_late_results():
    async def run():
        class Controlled(Driver):
            def __init__(self):
                self.started = {scope: asyncio.Event() for scope in ("a", "b")}
                self.release = asyncio.Event()
                self.main_started = asyncio.Event()

            async def run_background(self, agent, context, tool):
                self.started[context["task_id"]].set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()
                return BackgroundResult("LATE_PRIVATE")

            async def stream_segment(self, context, emit, tool):
                if context["task_id"] == "b":
                    await emit("Проверяю условия.")
                    self.main_started.set()
                    await self.release.wait()
                    return SegmentResult("Проверяю условия.")
                return await super().stream_segment(context, emit, tool)

        driver = Controlled()
        async with kernel(driver) as runtime:
            sid = (await runtime.create(CreateSession(agents=[
                AgentSpec(agent_id="research", instructions="Check policy", reads=["policy"]),
            ])))["session_id"]
            await runtime.submit(sid, TurnRequest(request_id=uuid4(), task_id="a", text="Полис A"))
            await completed(runtime.main_tasks[sid])
            await completed(asyncio.create_task(driver.started["a"].wait()))
            second = await runtime.submit(sid, TurnRequest(request_id=uuid4(), task_id="b", text="Полис B"))
            await completed(asyncio.create_task(driver.main_started.wait()))
            await completed(asyncio.create_task(driver.started["b"].wait()))
            pause = TaskRequest(request_id=uuid4(), task_id="a", status="paused")
            await runtime.update_task(sid, pause)
            assert not runtime.main_tasks[sid].cancelling()
            cancellation = BackgroundCancelRequest(request_id=uuid4(), task_id="b")
            first = await runtime.cancel_background(sid, cancellation)
            assert await runtime.cancel_background(sid, cancellation) == first
            assert not runtime.main_tasks[sid].cancelling()
            await runtime.update_task(sid, TaskRequest(request_id=uuid4(), task_id="b", status="paused"))
            driver.release.set()
            await completed(runtime.main_tasks[sid])
            await asyncio.gather(*(completed(task) for task in runtime.background_tasks[sid].values()))
            state = await runtime.repo.get(sid)
            assert state["responses"][second["response_id"]]["status"] == "interrupted"
            assert not state["background"]
            assert "LATE_PRIVATE" not in str(await runtime.repo.events(sid))
            await runtime.update_task(sid, TaskRequest(request_id=uuid4(), task_id="a", status="active"))
            assert (await runtime.list_tasks(sid))["active_task_id"] == "a"

    asyncio.run(run())


def test_interrupt_previous_is_atomic_idempotent_and_blocks_late_delta():
    async def run():
        class Controlled(Driver):
            def __init__(self):
                self.started = asyncio.Event()

            async def stream_segment(self, context, emit, tool):
                if context["input_revision"] == 1:
                    await emit("Старый префикс.")
                    self.started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        await emit("LATE_DELTA")
                    return SegmentResult("Старый префикс.")
                return await super().stream_segment(context, emit, tool)

        driver = Controlled()
        async with kernel(driver) as runtime:
            sid = (await runtime.create(CreateSession(agents=[])))["session_id"]
            first = await runtime.submit(sid, TurnRequest(request_id=uuid4(), text="Первый вопрос"))
            await completed(asyncio.create_task(driver.started.wait()))
            old_task = runtime.main_tasks[sid]
            with pytest.raises(KernelError, match="Сначала прервите"):
                await runtime.submit(sid, TurnRequest(request_id=uuid4(), text="Обычный ввод"))
            request = TurnRequest(request_id=uuid4(), text="Исправление", interrupt_previous=True,
                                  updates=[{"key": "city", "value": "Astana"}])
            accepted = await runtime.submit(sid, request)
            assert await runtime.submit(sid, request) == accepted
            await completed(runtime.main_tasks[sid])
            await completed(old_task)
            state = await runtime.repo.get(sid)
            old = state["responses"][first["response_id"]]
            assert old["status"] == "interrupted" and old["played_ms"] is None
            assert old["segments"][0]["text"] == "Старый префикс."
            assert state["input_revision"] == 2 and len(state["messages"]) == 2
            city = select_records(state, "default", ["city"])
            assert len(city) == 1 and city[0]["version"] == 1
            events = await runtime.repo.events(sid)
            assert "LATE_DELTA" not in str(events)
            stops = [item for item in events if item["type"] == "response.interrupted"]
            assert len(stops) == 1
            # Explicit null is shared scope; omitted scope means the current task.
            shared = TurnRequest.model_validate({**request.model_dump(), "updates": [
                {"key": "city", "value": "Astana", "task_id": None},
            ]})
            with pytest.raises(KernelError, match="request_id"):
                await runtime.submit(sid, shared)
            ingress_id = uuid4()
            await runtime.update_inputs(sid, request_id=ingress_id,
                                        updates=[{"key": "city", "value": "Almaty"}])
            with pytest.raises(KernelError, match="request_id"):
                await runtime.update_inputs(sid, request_id=ingress_id, updates=[
                    {"key": "city", "value": "Almaty", "task_id": None},
                ])
            with pytest.raises(KernelError) as unknown:
                await runtime.list_records(sid, "nonexistent-task")
            assert unknown.value.status == 404

    asyncio.run(run())
