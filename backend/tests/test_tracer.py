import secrets
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider

from app.tracer import TraceMiddleware, api_router, setup_tracing, span
from app.tracer.store import SpanStore

setup_tracing(otlp_endpoint=None)  # идемпотентно; без OTLP env — только локальное хранилище

app = FastAPI()
app.add_middleware(TraceMiddleware)
app.include_router(api_router)


@app.post("/calls/{session_id}/turns/text")
async def turn(session_id: str):
    async def events():
        with span("turn", {"session.id": session_id, "turn.id": 1}):
            with span("router", {"router.decision": "route"}):
                pass
            yield b"data: a\n\n"
            with span("responder"):
                pass
            yield b"data: b\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


def _flatten(nodes):
    for node in nodes:
        yield node
        yield from _flatten(node["children"])


def test_trace_continues_traceparent_and_nests_streamed_spans():
    trace_id, parent = secrets.token_hex(16), secrets.token_hex(8)
    sid = str(uuid4())
    with TestClient(app) as client:
        r = client.post(f"/calls/{sid}/turns/text",
                        headers={"traceparent": f"00-{trace_id}-{parent}-01"})
        assert r.text == "data: a\n\ndata: b\n\n"
        assert r.headers["x-trace-id"] == trace_id
        assert r.headers["traceparent"].startswith(f"00-{trace_id}-")

        body = client.get(f"/traces/{trace_id}").json()
        (server,) = body["spans"]
        assert server["name"] == "POST /calls/{session_id}/turns/text"
        assert server["parent_span_id"] == parent
        assert server["kind"] == "server"
        assert server["attributes"]["http.response.status_code"] == 200
        (turn_span,) = server["children"]
        assert turn_span["name"] == "turn"
        assert [c["name"] for c in turn_span["children"]] == ["router", "responder"]
        assert server["end_time"] >= turn_span["end_time"]  # закрыт после потока

        found = client.get("/traces", params={"session_id": sid}).json()
        assert [t["trace_id"] for t in found] == [trace_id]
        assert found[0]["span_count"] == 4 and found[0]["turn_id"] == "1"


def test_error_is_recorded_and_reraised():
    trace_id = secrets.token_hex(16)
    err_app = FastAPI()
    err_app.add_middleware(TraceMiddleware)
    err_app.include_router(api_router)

    @err_app.get("/boom")
    async def boom():
        with span("executor"):
            raise ValueError("bad")

    with TestClient(err_app, raise_server_exceptions=False) as client:
        r = client.get("/boom", headers={"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"})
        assert r.status_code == 500
        spans = list(_flatten(client.get(f"/traces/{trace_id}").json()["spans"]))
    executor = next(s for s in spans if s["name"] == "executor")
    assert executor["status"] == "error"
    assert executor["events"][0]["name"] == "exception"


def _local_store(max_spans: int) -> tuple[SpanStore, TracerProvider]:
    store = SpanStore(max_spans=max_spans)
    provider = TracerProvider()
    provider.add_span_processor(store)
    return store, provider


def test_ring_buffer_evicts_oldest_traces():
    store, provider = _local_store(max_spans=4)
    tracer = provider.get_tracer("test")
    ids = []
    for _ in range(3):
        with (tracer.start_as_current_span("turn", attributes={"session.id": "s"}) as root,
              tracer.start_as_current_span("router")):
            pass
        ids.append(f"{root.get_span_context().trace_id:032x}")
    assert store.trace_ids() == ids[1:]
    assert store.trace_ids("s") == ids[1:]
    assert store.spans(ids[0]) == []


def test_pii_is_masked():
    store, provider = _local_store(max_spans=10)
    with provider.get_tracer("test").start_as_current_span("executor", attributes={
        "client.phone": "+7 701 123 45 67",
        "note": "звоните +77011234567, пишите a.b@mail.kz, ИИН 900101300123",
        "client.id": "C004",
    }) as s:
        pass
    (record,) = store.spans(f"{s.get_span_context().trace_id:032x}")
    attrs = record["attributes"]
    assert attrs["client.phone"] == "[redacted]"
    assert attrs["note"] == "звоните [redacted], пишите [redacted], ИИН [redacted]"
    assert attrs["client.id"] == "C004"


def test_cancellation_is_recorded_and_propagated():
    import asyncio

    from app.tracer.setup import get_store

    started = []

    async def stage():
        with span("tts.sentence", seq=1) as s:
            started.append(f"{s.get_span_context().trace_id:032x}")
            await asyncio.sleep(10)

    async def main():
        task = asyncio.create_task(stage())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(main())
    (record,) = get_store().spans(started[0])
    assert [e["name"] for e in record["events"]] == ["cancelled"]
    assert record["status"] == "unset"
