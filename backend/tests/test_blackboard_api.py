"""Blackboard control HTTP contract on the real PostgreSQL repository."""

import asyncio
from uuid import uuid4

from test_kernel_api import client_for
from test_kernel_runtime import Driver, completed, kernel


def test_task_controls_record_conflicts_and_private_projection():
    async def run():
        async with kernel(Driver()) as runtime, client_for(runtime) as client:
            sid = (await client.post("/sessions", json={"agents": []})).json()["session_id"]
            base = f"/sessions/{sid}"
            task = {"request_id": str(uuid4()), "task_id": "claim:1", "title": "Claim"}
            created = await client.post(base + "/tasks", json=task)
            assert created.status_code == 200
            assert (await client.post(base + "/tasks", json=task)).json() == created.json()
            assert (await client.post(base + "/tasks", json={**task, "title": "Changed"})).status_code == 409
            request = {"request_id": str(uuid4()), "task_id": "claim:1", "key": "city", "value": "PRIVATE_CITY"}
            stored = await client.post(base + "/records", json=request)
            assert stored.status_code == 200
            record_id = stored.json()["record"]["record_id"]
            assert "PRIVATE_CITY" not in stored.text
            correction = {**request, "request_id": str(uuid4()), "value": "PRIVATE_CORRECTION",
                          "expected_record_id": record_id}
            assert (await client.post(base + "/records", json=correction)).status_code == 200
            stale = {**correction, "request_id": str(uuid4()), "value": "OLD"}
            assert (await client.post(base + "/records", json=stale)).status_code == 409
            records = await client.get(base + "/records?task_id=claim:1")
            assert records.status_code == 200 and "PRIVATE_" not in records.text
            assert (await client.get(base + "/records", params={"task_id": "bad scope"})).status_code == 422
            assert sum(record["active"] for record in records.json()["records"]) == 1
            paused = {"request_id": str(uuid4()), "task_id": "claim:1", "status": "paused"}
            assert (await client.post(base + "/tasks", json=paused)).status_code == 200
            turn = {"request_id": str(uuid4()), "text": "Продолжим", "task_id": "claim:1"}
            assert (await client.post(base + "/turns", json=turn)).status_code == 409
            resumed = {**paused, "request_id": str(uuid4()), "status": "active"}
            assert (await client.post(base + "/tasks", json=resumed)).status_code == 200
            assert (await client.post(base + "/turns", json=turn)).status_code == 202
            await completed(runtime.main_tasks[sid])
            cancellation = {"request_id": str(uuid4()), "task_id": "claim:1"}
            assert (await client.post(base + "/background/cancel", json=cancellation)).status_code == 200
            await client.post(base + "/close")
            public = await client.get(base + "/events")
            assert "PRIVATE_" not in public.text
            assert "blackboard.updated" not in public.text

    asyncio.run(run())
