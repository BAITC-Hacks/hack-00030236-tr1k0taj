"""Трасса хода через настоящее приложение: traceparent → сервер → turn → этапы (tracer-module.md)."""

import json
import secrets
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.router import RouterOutput, RouterResult


class FakeRouter:
    name = "fake"

    async def route(self, utterance, view, kb):
        output = RouterOutput.model_validate({
            "scenarios": [{"scenario_id": "SC17", "confidence": 0.9, "reason": "test"}],
            "language": "ru", "slots": {},
        })
        return RouterResult(output=output, model="fake", prompt="p", raw="r")


def _events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def _flatten(nodes):
    for node in nodes:
        yield node
        yield from _flatten(node["children"])


def test_text_turn_trace_nests_stages_under_server_span():
    trace_id, parent = secrets.token_hex(16), secrets.token_hex(8)
    sid = str(uuid4())
    with TestClient(app) as client:
        client.app.state.calls.providers.router = FakeRouter()
        r = client.post(f"/calls/{sid}/turns/text", json={"text": "статус выплаты"},
                        headers={"traceparent": f"00-{trace_id}-{parent}-01"})
        assert r.status_code == 200 and r.headers["x-trace-id"] == trace_id
        done = next(e for e in _events(r.text) if e["type"] == "turn.done")
        assert done["trace_id"] == trace_id

        trace = client.get(f"/traces/{trace_id}").json()
        (server,) = trace["spans"]
        assert server["name"] == "POST /calls/{session_id}/turns/text"
        assert server["parent_span_id"] == parent
        assert server["attributes"]["input.source"] == "text"
        (turn,) = [c for c in server["children"] if c["name"] == "turn"]
        assert turn["attributes"]["turn.id"] == done["turn_id"]
        assert turn["attributes"]["sse.events.turn.done"] == 1
        assert "latency.router_ms" in turn["attributes"]
        stages = [c["name"] for c in turn["children"]]
        assert stages[:3] == ["router", "executor", "responder"]
        router = turn["children"][0]["attributes"]
        assert router["router.scenario_id"] == "SC17"
        assert router["router.decision"] == "route"
        assert router["gen_ai.operation.name"] == "chat"

        spans = list(_flatten(trace["spans"]))
        # задачи kernel/TTS не утекли в чужой контекст: все span'ы — в этой трассе и с сессией
        assert all(s["attributes"].get("session.id") == sid for s in spans)
        names = {s["name"] for s in spans}
        assert "segment" in names  # kernel-ответ вложен под responder

        # фоновые agent.run — свои трассы той же сессии, связанные с ходом link'ом
        found = {t["trace_id"]: t for t in client.get("/traces", params={"session_id": sid}).json()}
        assert found[trace_id]["root_name"] == server["name"]
        for other in set(found) - {trace_id}:
            (run,) = client.get(f"/traces/{other}").json()["spans"]
            assert run["name"] == "agent.run" and run["links"][0]["trace_id"] == trace_id
