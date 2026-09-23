"""Streamed deltas are journal-only appends; the slot snapshot is written by durable changes."""

import asyncio
from uuid import uuid4

import pytest

from app.config import settings
from app.context import Contexts, MemoryStore
from app.kernel import Repository, Runtime
from app.kernel.provider import SegmentResult
from app.kernel.types import CreateSession, TurnRequest

CHUNKS = ["Заявление ", "принято, ", "статус ", "проверяется."]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "embeddings_enabled", False)


class CountingStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.saved, self.appended = [], []

    async def save(self, ctx, entries):
        self.saved.append([e.payload.get("type") for e in entries])
        await super().save(ctx, entries)

    async def append(self, entries):
        self.appended.append([e.payload.get("type") for e in entries])
        await super().append(entries)

    def journal_payloads(self, sid):
        return [e.payload for e in self.entries if e.session_id == sid and e.type == "kernel"]


class Streaming:
    def __init__(self, block=False):
        self.block, self.streamed = block, asyncio.Event()

    async def stream_segment(self, context, emit, tool):
        for chunk in CHUNKS:
            await emit(chunk)
        self.streamed.set()
        if self.block:
            await asyncio.Event().wait()
        return SegmentResult("".join(CHUNKS))

    async def run_background(self, agent, context, tool):
        raise AssertionError("no background agents in this test")

    async def close(self):
        pass


def segment(slot, rid):
    return slot["responses"][rid]["segments"][0]


def test_deltas_skip_state_upserts_but_reach_journal_and_final_state():
    async def run():
        store = CountingStore()
        runtime = Runtime(Streaming(), repository=Repository(contexts=Contexts(store)))
        try:
            sid = (await runtime.create(CreateSession(agents=[])))["session_id"]
            rid = (await runtime.submit(sid, TurnRequest(request_id=uuid4(), text="Статус?")))[
                "response_id"]
            await asyncio.wait_for(asyncio.shield(runtime.main_tasks[sid]), timeout=5)
        finally:
            await runtime.shutdown()
        # Before: one full JSONB upsert per delta. After: deltas are journal-only appends.
        assert not any("response.delta" in types for types in store.saved)
        assert store.appended == [["response.delta"]] * len(CHUNKS)
        deltas = [e["payload"]["text"] for e in store.journal_payloads(sid)
                  if e["type"] == "response.delta"]
        assert deltas == CHUNKS
        slot = store.states[sid].kernel
        assert segment(slot, rid)["text"] == "".join(CHUNKS)
        assert segment(slot, rid)["status"] == "completed"

    asyncio.run(run())


def test_restart_after_journal_only_appends_keeps_seq_monotonic():
    async def run():
        store = CountingStore()
        driver = Streaming(block=True)
        runtime = Runtime(driver, repository=Repository(contexts=Contexts(store)))
        sid = (await runtime.create(CreateSession(agents=[])))["session_id"]
        rid = (await runtime.submit(sid, TurnRequest(request_id=uuid4(), text="Статус?")))[
            "response_id"]
        await asyncio.wait_for(driver.streamed.wait(), timeout=5)
        runtime.main_tasks[sid].cancel()  # "crash": the segment never completes durably

        # Trade-off: the stored snapshot lags, the journal has every delta.
        assert segment(store.states[sid].kernel, rid)["text"] == ""
        assert store.states[sid].kernel["last_seq"] < store.journal_payloads(sid)[-1]["seq"]
        restarted = Runtime(Streaming(), repository=Repository(contexts=Contexts(store)))
        await restarted.recover()
        slot = await restarted.repo.get(sid)
        assert slot["status"] == "closed"
        journal = await restarted.repo.events(sid, public=False, limit=1000)
        seqs = [e["seq"] for e in journal]
        assert seqs == sorted(set(seqs)) and slot["last_seq"] == seqs[-1]
        text = "".join(e["payload"]["text"] for e in journal
                       if e["type"] == "response.delta" and e["response_id"] == rid)
        assert text == "".join(CHUNKS)
        await restarted.shutdown()

    asyncio.run(run())
