"""One owner, one lock, private runtime projection and persistent event replay."""

import asyncio

import pytest

from app.context import Contexts, MemoryStore
from app.kernel.store import Repository, event


def test_shared_owner_restart_replay_and_private_projection():
    async def run():
        store = MemoryStore()
        contexts = Contexts(store)
        await contexts.start_call("shared")
        repo = Repository(contexts=contexts)
        state = await repo.create([], {}, "mock", session_id="shared")

        def publish(state, seq):
            state["input_revision"] += 1
            return seq, [event("agent.result", {"summary": "PRIVATE_RESULT"}),
                         event("response.delta", {"text": "Public"}, public=True)]

        async def domain_change():
            async with contexts.mutate("shared") as mutation:
                mutation.set_client("C004")

        await asyncio.gather(repo.change("shared", publish), domain_change())
        snapshot = await contexts.snapshot("shared")
        assert snapshot.client_id == "C004" and snapshot.kernel["input_revision"] == 1
        assert "kernel" not in snapshot.model_dump()
        assert "PRIVATE_RESULT" not in str([entry.model_dump() for entry in await contexts.board("shared")])
        assert len(await repo.events("shared")) == 2
        assert len(await repo.events("shared", public=False)) == 3
        assert [e["seq"] for e in await repo.events("shared", after=state["last_seq"])] == [3]

        # A fresh owner reloads the same context and journal instead of making a second session.
        restarted = Repository(contexts=Contexts(store))
        assert await restarted.get("shared") == await repo.get("shared")
        assert await restarted.open_sessions() == ["shared"]
        assert (await restarted.contexts.snapshot("shared")).client_id == "C004"

    asyncio.run(run())


def test_reset_generation_cursor_and_atomic_failed_mutation():
    async def run():
        store = MemoryStore()
        contexts = Contexts(store)
        repo = Repository(contexts=contexts)
        state = await repo.create([], {}, "mock", session_id="shared")
        generation = state["generation"]

        def fail(state, seq):
            state["input_revision"] = 900
            raise ValueError("roll back")

        with pytest.raises(ValueError, match="roll back"):
            await repo.change("shared", fail)
        assert await repo.get("shared") == state
        assert len(await repo.events("shared", public=False)) == 1

        resets = []
        contexts.on_reset(lambda sid, generation: resets.append((sid, generation)))
        await contexts.start_call("shared")
        reset = await repo.get("shared")
        assert reset["generation"] == generation + 1 and reset["status"] == "closed"
        assert reset["messages"] == [] and reset["last_seq"] > state["last_seq"]
        reopened = await repo.create([], {}, "mock", session_id="shared")
        assert reopened["generation"] == reset["generation"]
        assert reopened["last_seq"] > reset["last_seq"]
        assert resets == [("shared", generation + 1)]
        assert await repo.create([], {}, "mock", session_id="shared") == reopened

        def close_with_old_work(state, seq):
            state["messages"].append({"text": "old"})
            state["status"] = "closed"
            return None, [event("session.closed", public=True)]

        await repo.change("shared", close_with_old_work)
        next_call = await repo.create([], {}, "mock", session_id="shared")
        assert next_call["generation"] == reopened["generation"] + 1
        assert next_call["messages"] == []

    asyncio.run(run())
