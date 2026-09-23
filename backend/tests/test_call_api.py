"""Ход звонка через HTTP: SSE-события, моки без ключей, один foreground-ход."""

import json

from fastapi.testclient import TestClient

from app.main import app
from app.router import RouterOutput, RouterResult


def events(body: str) -> list[dict]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if "data" in lines:
            out.append(json.loads(lines["data"]))
    return out


class FakeRouter:
    name = "fake"

    def __init__(self, scenario_id: str, confidence: float = 0.9):
        self.pick = {"scenario_id": scenario_id, "confidence": confidence, "reason": "test"}

    async def route(self, utterance, view, kb):
        output = RouterOutput.model_validate(
            {"scenarios": [self.pick], "language": "ru", "slots": {"phone": None}}
        )
        return RouterResult(output=output, model="fake", prompt="p", raw="r")


def start(client) -> str:
    r = client.post("/calls")
    assert r.status_code == 201
    return r.json()["session_id"]


def test_mock_mode_turn_does_not_crash():
    with TestClient(app) as client:
        sid = start(client)
        r = client.post(f"/calls/{sid}/turns/text", json={"text": "что с заявлением?"})
        assert r.status_code == 200
        evs = events(r.text)
        types = [e["type"] for e in evs]
        assert types[:2] == ["transcript", "turn.started"]
        assert {"type": "error", "code": "llm_unavailable"}.items() <= evs[2].items()
        assert types[-1] == "turn.done" and "reply.done" in types

        audio = client.post(f"/calls/{sid}/turns/audio", files={"audio": ("a.webm", b"x")})
        assert events(audio.text)[0]["code"] == "stt_unavailable"


def test_routed_turn_streams_reply_and_bumps_version():
    with TestClient(app) as client:
        # lifespan собирает провайдеров заново на каждый TestClient, откат не нужен
        client.app.state.calls.providers.router = FakeRouter("SC17")
        sid = start(client)
        evs = events(client.post(f"/calls/{sid}/turns/text", json={"text": "статус"}).text)
        by_type = {e["type"]: e for e in evs}
        assert by_type["routing"]["decision"] == "route"
        assert by_type["routing"]["scenarios"][0]["scenario_id"] == "SC17"
        assert any(e["type"] == "reply.delta" for e in evs)
        done = by_type["turn.done"]
        assert done["context_version"] > by_type["turn.started"]["context_version"]
        assert done["latency_ms"]["router"] is not None

        ctx = client.get(f"/sessions/{sid}/context").json()
        assert ctx["active_scenario"] == "SC17"
        debug = client.get(f"/calls/{sid}/router/last").json()
        assert debug["result"]["prompt"] == "p"


def test_unknown_scenario_is_an_error_not_a_guess():
    with TestClient(app) as client:
        client.app.state.calls.providers.router = FakeRouter("SC99")
        sid = start(client)
        evs = events(client.post(f"/calls/{sid}/turns/text", json={"text": "x"}).text)
        assert any(e["type"] == "error" and e["code"] == "router_invalid" for e in evs)
        assert not any(e["type"] == "routing" for e in evs)


def test_busy_session_and_missing_session():
    with TestClient(app) as client:
        sid = start(client)
        calls = client.app.state.calls
        lock = calls._locks[sid]
        client.portal.call(lock.acquire)
        try:
            r = client.post(f"/calls/{sid}/turns/text", json={"text": "привет"})
            assert r.status_code == 409
        finally:
            lock.release()
        assert client.post("/calls/nope/turns/text", json={"text": "x"}).status_code == 404


def test_cancelled_turn_emits_nothing_after_stop():
    with TestClient(app) as client:
        calls = client.app.state.calls
        calls.providers.router = FakeRouter("SC17")
        sid = start(client)

        async def run():
            gen = calls.run_turn(sid, text="статус")
            async for e in gen:
                if e.type == "turn.started":
                    await calls.contexts.cancel_turn(sid, e.turn_id)
                    break
            return [e.type async for e in gen]

        assert client.portal.call(run) == ["turn.cancelled"]
        board = client.get(f"/sessions/{sid}/board").json()
        assert any(e["type"] == "turn" and e["payload"]["status"] == "cancelled" for e in board)
