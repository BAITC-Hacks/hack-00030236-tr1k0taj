import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.tracer import (
    api_router,
    flush_traces,
    instrument_sqlalchemy,
    setup_tracing,
    span,
    start_persistence,
    stop_persistence,
    trace_spans,
)
from app.tracer.setup import get_store

setup_tracing(otlp_endpoint=None)

app = FastAPI()
app.include_router(api_router)


def _tid(s) -> str:
    return f"{s.get_span_context().trace_id:032x}"


def _sid(s) -> str:
    return f"{s.get_span_context().span_id:016x}"


@asynccontextmanager
async def persisted():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    instrument_sqlalchemy(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await start_persistence(factory, interval=60)  # сбрасываем явно через flush_traces()
    traces: set[str] = set()
    try:
        yield factory, traces
    finally:
        await stop_persistence()
        async with factory() as db:
            await db.execute(delete(trace_spans).where(trace_spans.c.trace_id.in_(traces)))
            await db.commit()
        await engine.dispose()


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def one_turn(sid: str, turn: int = 1) -> str:
    with span("turn", {"session.id": sid, "turn.id": turn, "input.source": "text"}) as t:
        with span("stt", {"local.transcript": "продлить полис, мой номер +77011234567",
                          "client.phone": "+7 701 123 45 67"}):
            pass
        router = {"router.decision": "route", "router.scenario_id": "POLICY_RENEW",
                  "router.confidence": 0.91}
        llm = {"gen_ai.request.model": "gpt-4.1-mini", "gen_ai.usage.input_tokens": 100,
               "gen_ai.usage.output_tokens": 20}
        with span("router", router), span("llm.call", llm):
            pass
        with span("responder"):
            for model, tin, tout in (("gpt-4.1", 50, 30), ("gpt-4.1-mini", 10, 5)):
                with span("llm.call", {"gen_ai.request.model": model,
                                       "gen_ai.usage.input_tokens": tin,
                                       "gen_ai.usage.output_tokens": tout}):
                    pass
    return _tid(t)


def test_session_trace_roundtrip_through_db_with_redaction():
    sid = str(uuid4())

    async def main():
        async with persisted() as (factory, traces):
            traces.add(one_turn(sid))
            assert await flush_traces() == 7
            get_store().clear()  # дальше читаем только из БД

            async with client() as c:
                body = (await c.get(f"/traces/sessions/{sid}")).json()
                missing = await c.get(f"/traces/sessions/{uuid4()}")
            async with factory() as db:
                stt = (await db.execute(select(trace_spans.c.attributes).where(
                    trace_spans.c.name == "stt", trace_spans.c.session_id == sid))).scalar_one()
            return body, missing.status_code, stt

    body, missing, stt = asyncio.run(main())
    assert missing == 404
    assert stt["client.phone"] == "[redacted]"
    assert stt["local.transcript"] == "продлить полис, мой номер [redacted]"

    summary = body["summary"]
    assert summary["turns"] == 1 and summary["traces"] == 1 and summary["llm_calls"] == 3
    assert summary["tokens"] == {"input": 160, "output": 55, "total": 215}
    assert summary["tokens_by_model"] == {
        "gpt-4.1-mini": {"input": 110, "output": 25, "calls": 2},
        "gpt-4.1": {"input": 50, "output": 30, "calls": 1},
    }
    assert summary["scenarios"] == ["POLICY_RENEW"]
    (turn,) = body["turns"]
    assert turn["turn_id"] == 1 and turn["input_source"] == "text"
    assert turn["transcript"] == "продлить полис, мой номер [redacted]"
    assert turn["router"] == {"decision": "route", "scenario_id": "POLICY_RENEW",
                              "confidence": 0.91}
    assert set(turn["stages"]) == {"stt", "router", "responder"}
    assert turn["tokens_by_model"]["gpt-4.1"]["calls"] == 1


def test_spans_without_session_are_attributed_by_trace():
    sid = str(uuid4())

    async def main():
        async with persisted() as (factory, traces):
            with span("POST /calls/{session_id}/turns/audio") as root:
                with span("decode") as early:  # закрыт до того, как появился session.id
                    pass
                await flush_traces()
                with span("turn", {"session.id": sid, "turn.id": 2}):
                    pass
            traces.add(_tid(root))
            await flush_traces()
            async with factory() as db:
                rows = dict((await db.execute(select(trace_spans.c.span_id, trace_spans.c.session_id)
                                              .where(trace_spans.c.trace_id == _tid(root)))).all())
            async with client() as c:
                body = (await c.get(f"/traces/sessions/{sid}")).json()
            return rows, _sid(early), _sid(root), body

    rows, early, root, body = asyncio.run(main())
    assert rows[early] == sid and rows[root] == sid  # backfill в БД и подсказка по трассе
    assert body["traces"][0]["span_count"] == 3
    assert body["turns"][0]["stages"] == {}


def test_history_lists_newest_sessions_first():
    first, second = str(uuid4()), str(uuid4())

    async def main():
        async with persisted() as (_, traces):
            traces.add(one_turn(first))
            await asyncio.sleep(0.01)
            traces.add(one_turn(second, turn=1))
            await flush_traces()
            async with client() as c:
                return (await c.get("/traces/sessions", params={"limit": 200})).json()

    history = asyncio.run(main())
    ids = [s["session_id"] for s in history]
    assert ids.index(second) < ids.index(first)
    entry = history[ids.index(second)]
    assert entry["last_transcript"] == "продлить полис, мой номер [redacted]"
    assert entry["tokens"]["total"] == 215


def test_sql_spans_nest_under_current_span_and_flush_is_not_traced():
    async def main():
        async with persisted() as (factory, traces):
            with span("executor") as ex:
                async with factory() as db:
                    await db.execute(text("SELECT 1 AS one"))
            with span("outer") as outer:
                await flush_traces()
            traces.update({_tid(ex), _tid(outer)})
            store = get_store()
            return store.spans(_tid(ex)), store.spans(_tid(outer)), _sid(ex)

    ex_spans, outer_spans, ex_id = asyncio.run(main())
    (db_span,) = [s for s in ex_spans if s["name"] == "db.query"]
    assert db_span["parent_span_id"] == ex_id
    assert db_span["kind"] == "client"
    assert db_span["attributes"]["db.operation.name"] == "SELECT"
    assert db_span["attributes"]["db.system.name"] == "postgresql"
    assert [s["name"] for s in outer_spans] == ["outer"]  # INSERT трасс не породил span'ов
