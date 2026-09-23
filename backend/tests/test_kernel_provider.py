import asyncio
import json
from types import SimpleNamespace

import pytest

from app.kernel.provider import ModelDriver, ProviderError, _redact, _TextDecoder


def event(kind, **values):
    return SimpleNamespace(type=kind, **values)


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.completed = False

    async def __aiter__(self):
        for item in self.events:
            if item.type == "response.completed":
                self.completed = True
            yield item

    async def close(self):
        self.closed = True


class Responses:
    def __init__(self, streams):
        self.streams = list(streams)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.streams.pop(0)


def driver_with(*streams, **kwargs):
    driver = ModelDriver(mock=False, **kwargs)
    driver._client = SimpleNamespace(responses=Responses(streams))
    return driver


def text_stream(payload, chunk_size=4):
    raw = json.dumps(payload, ensure_ascii=True)
    return Stream(
        [event("response.output_text.delta", delta=raw[i:i + chunk_size])
         for i in range(0, len(raw), chunk_size)]
        + [event("response.completed", response={"output": []})]
    )


def tools_stream(name="rag_search", arguments=None):
    return Stream([event("response.completed", response={"output": [{
        "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": name,
        "arguments": json.dumps(arguments or {
            "query": "страховой случай", "kinds": ["kb"], "limit": 3,
        }),
    }]})])


async def no_tools(name, arguments):
    raise AssertionError("No tool should be called")


@pytest.mark.parametrize("chunk_size", range(1, 18))
def test_incremental_json_handles_split_escapes_and_unicode(chunk_size):
    value = 'Полис «Сақта» \\ "x"\n\t☀️ 😀 соңында'
    raw = json.dumps({"text": value, "continue_response": False, "used_source_ids": ["kb:x"]})
    decoder = _TextDecoder()
    pieces = [decoder.feed(raw[i:i + chunk_size]) for i in range(0, len(raw), chunk_size)]
    assert "".join(pieces) == value
    assert decoder.done
    assert decoder.text == value
    assert "continue_response" not in "".join(pieces)


def test_real_driver_streams_before_completion_and_roundtrips_tools():
    async def check():
        first = tools_stream()
        final = text_stream({
            "text": "Заявление принимается в офисе.",
            "continue_response": True, "used_source_ids": ["kb:claims.submission"],
        })
        driver = driver_with(first, final)
        emitted, calls = [], []

        async def emit(delta):
            assert not final.completed
            emitted.append(delta)

        async def runner(name, arguments):
            calls.append((name, arguments))
            return {"hits": [{"source_id": "kb:claims.submission", "payload": {
                "title": "Заявление принимается в офисе.",
            }}]}

        result = await driver.stream_segment({"user_text": "Как подать заявление?"}, emit, runner)
        assert result.text == "".join(emitted) == "Заявление принимается в офисе."
        assert len(emitted) > 2
        assert result.continue_response
        assert result.used_source_ids == ["kb:claims.submission"]
        assert calls == [("rag_search", {"query": "страховой случай", "kinds": ["kb"], "limit": 3})]
        assert first.closed and final.closed
        requests = driver._client.responses.requests
        assert all(request["store"] is False for request in requests)
        assert all(request["stream"] is True for request in requests)
        assert all(tool["strict"] for tool in requests[0]["tools"])
        assert requests[1]["input"][-1]["type"] == "function_call_output"
        assert requests[1]["input"][-1]["call_id"] == "call_1"
        assert "kb:claims.submission" in requests[1]["input"][-1]["output"]

    asyncio.run(check())


def test_main_reads_new_background_and_suppresses_repeated_prefix():
    async def check():
        stream = text_stream({
            "text": "Здравствуйте. Заявление принимается в офисе.",
            "continue_response": False, "used_source_ids": ["kb:claims.submission"],
        }, chunk_size=1)
        driver = driver_with(stream)
        emitted = []

        async def emit(delta):
            emitted.append(delta)

        context = {
            "user_text": "Как подать заявление?", "prefix": "Здравствуйте. ", "segment_index": 1,
            "background": [{"summary": "Приём в офисе", "source_ids": ["kb:claims.submission"]}],
        }
        result = await driver.stream_segment(context, emit, no_tools)
        assert result.text == "".join(emitted) == "Заявление принимается в офисе."
        request = driver._client.responses.requests[0]
        sent = json.loads(request["input"][0]["content"])
        assert sent["background"] == context["background"]
        assert sent["prefix"] == context["prefix"]

    asyncio.run(check())


@pytest.mark.parametrize("tool_name,args", [
    ("delete_record", {"kind": "kb", "key": "x"}),
    ("rag_read", {"kind": "client", "key": "C004"}),
    ("rag_search", {"query": "полис", "kinds": ["kb"], "limit": 999}),
])
def test_invalid_tools_never_reach_runner(tool_name, args):
    async def check():
        driver = driver_with(
            tools_stream(tool_name, args),
            text_stream({"summary": "Нет данных", "facts": [], "source_ids": []}),
        )
        result = await driver.run_background(
            {"instructions": "Найди документы", "tools": ["rag_search", "rag_read"]},
            {"user_text": "полис"}, no_tools,
        )
        assert result.facts == []
        tool_output = driver._client.responses.requests[1]["input"][-1]
        assert "invalid_tool_arguments_or_permission" in tool_output["output"]

    asyncio.run(check())


def test_tool_rounds_are_bounded_and_force_final_response():
    async def check():
        driver = driver_with(tools_stream(), tools_stream(), max_tool_rounds=1)
        called = []

        async def runner(name, args):
            called.append(name)
            return {"hits": []}

        with pytest.raises(ProviderError, match="tool_round_limit"):
            await driver.run_background({"instructions": "Найди документы"}, {}, runner)
        assert called == ["rag_search"]
        assert driver._client.responses.requests[-1]["tool_choice"] == "none"

    asyncio.run(check())


def test_cancellation_closes_underlying_stream_without_more_deltas():
    async def check():
        emitted = []
        first_delta = asyncio.Event()

        class HangingStream(Stream):
            async def __aiter__(self):
                yield event("response.output_text.delta", delta='{"text":"Полис')
                first_delta.set()
                await asyncio.Event().wait()
                yield event("response.output_text.delta", delta="после отмены")

        stream = HangingStream([])
        driver = driver_with(stream)

        async def emit(delta):
            emitted.append(delta)

        task = asyncio.create_task(driver.stream_segment({}, emit, no_tools))
        await first_delta.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        assert emitted == ["Полис"]

    asyncio.run(check())


def test_background_rejects_invented_sources_and_unsourced_facts():
    async def check():
        for payload in (
            {"summary": "Условие", "facts": [], "source_ids": ["kb:invented"]},
            {"summary": "Условие", "facts": [{"text": "Выдумка", "source_ids": []}],
             "source_ids": []},
        ):
            driver = driver_with(text_stream(payload))
            with pytest.raises(ProviderError):
                await driver.run_background({"instructions": "Найди документы"}, {}, no_tools)

    asyncio.run(check())


def test_missing_key_is_explicit_and_mock_is_labelled():
    async def check():
        async def emit(delta):
            pass

        with pytest.raises(ProviderError, match="missing_api_key"):
            await ModelDriver(mock=False).stream_segment({}, emit, no_tools)
        result = await ModelDriver(mock=True).stream_segment({"segment_index": 0}, emit, no_tools)
        assert "без LLM" in result.text
        assert result.continue_response

    asyncio.run(check())


def test_redaction_preserves_domain_and_source_identifiers():
    result = _redact({
        "client_id": "C004", "scenario_id": "SC17", "source_id": "kb:claims.submission",
        "full_name": "Клиент", "phone": "+7 701 123 45 67", "email": "client@example.com",
        "text": "Мой ИИН 123456789012, телефон +7 (701) 123-45-67, a@example.com",
    })
    assert result["client_id"] == "C004"
    assert result["scenario_id"] == "SC17"
    assert result["source_id"] == "kb:claims.submission"
    assert result["full_name"] == result["phone"] == result["email"] == "[redacted]"
    assert "123456789012" not in result["text"]
    assert "123-45-67" not in result["text"]
    assert "@" not in result["text"]


def test_local_phone_prefix_and_structured_address_are_redacted():
    from app.kernel.provider import _redact

    result = _redact({"text": "Телефон 8 701 000 00 04", "address": "Real private address"})
    assert "701" not in result["text"]
    assert result["address"] == "[redacted]"
