"""Explicit HTTP + PostgreSQL smoke. --live prompts securely, never writes an API key."""

import argparse
import asyncio
import getpass
import json
import os
import socket
import sys
import time
from uuid import uuid4

import httpx


async def run(live):
    from app.config import settings
    from app.db import engine
    from app.knowledge import open_knowledge, reindex_knowledge

    if live:
        result = await reindex_knowledge()
        print("Index:", json.dumps(result), flush=True)
        assert result["status"] == "complete", "Live indexing did not complete"
        async with open_knowledge() as kb:
            for query in ("Куда отправить фото акта?", "Құжаттың фотосын қайда жіберемін?", "Фото акта қайда жіберемін?"):
                result = await kb.search.rag_query(query, ["kb"], 3)
                assert result["search_mode"] == "hybrid" and result["hits"], result
                print("RAG:", query, "=>", [h["source_id"] for h in result["hits"]], flush=True)
    await engine.dispose()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "MOCK_MODE": str(not live).lower(),
           "EMBEDDINGS_ENABLED": str(live).lower()}
    server = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log", "--log-level", "warning",
        env=env, stdout=asyncio.subprocess.DEVNULL,
    )
    started = time.monotonic()
    sid = None
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=90) as client:
            for _ in range(100):
                try:
                    if (await client.get("/health")).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                if server.returncode is not None:
                    raise RuntimeError("Smoke API server failed to start")
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Smoke API server not ready")
            capabilities = (await client.get("/kernel/capabilities")).json()
            assert capabilities["mode"] == ("live" if live else "mock")
            created = await client.post("/sessions", json={})
            created.raise_for_status()
            sid = created.json()["session_id"]
            started = time.monotonic()
            submitted = await client.post(f"/sessions/{sid}/turns", json={
                "request_id": str(uuid4()),
                "text": "Объясните двумя короткими предложениями: можно отправить фото документа по страховому обращению и как это сделать?",
            })
            submitted.raise_for_status()
            rid = submitted.json()["response_id"]
            output, first_delta, event_count, completed = [], None, 0, False
            async with client.stream("GET", f"/sessions/{sid}/events") as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    item = json.loads(line[6:])
                    event_count += 1
                    assert item["visibility"] == "public" and not item["type"].startswith(("agent.", "tool."))
                    if item["type"] == "response.delta":
                        first_delta = first_delta or time.monotonic() - started
                        output.append(item["payload"]["text"])
                    if item["type"] == "response.failed":
                        raise RuntimeError("Live response failed: " + item["payload"]["code"])
                    if item["type"] == "response.completed":
                        completed = True
                        break
            assert completed and len(output) > 1, "No incremental output"
            saved = (await client.get(f"/sessions/{sid}/history")).json()["messages"][-1]
            assert saved["response_id"] == rid
            if live:
                assert any(s["used_source_ids"] for s in saved["segments"]), "Answer lacks source references"
            assert all(s["delivery"]["status"] == "unknown" for s in saved["segments"])
            timings = [{"segment_id": seg["segment_id"], "start_ms": i * 1000, "end_ms": (i+1) * 1000}
                       for i, seg in enumerate(saved["segments"])]
            playback = await client.post(f"/sessions/{sid}/playback", json={
                "request_id": str(uuid4()), "response_id": rid, "played_ms": 500, "segments": timings,
            })
            playback.raise_for_status()
            stopped = await client.post(f"/sessions/{sid}/interrupt", json={
                "request_id": str(uuid4()), "response_id": rid, "played_ms": 500,
            })
            stopped.raise_for_status()
            saved = (await client.get(f"/sessions/{sid}/history")).json()["messages"][-1]
            assert saved["status"] == "interrupted"
            assert saved["segments"][0]["delivery"]["status"] == "partially_heard"
            assert all(s["delivery"]["status"] == "unheard" for s in saved["segments"][1:])
            await client.post(f"/sessions/{sid}/close")
            replayed = await client.get(f"/sessions/{sid}/events")
            assert "session.closed" in replayed.text and "agent.result" not in replayed.text
            print(json.dumps({"mode": capabilities["mode"], "public_events": event_count,
                "text_deltas": len(output), "first_delta_seconds": round(first_delta, 3),
                "segments": len(saved["segments"]), "response_text": "".join(output),
                "source_ids": [source for segment in saved["segments"] for source in segment["used_source_ids"]],
                "checks": "HTTP SSE, history, playback, interrupt, replay, privacy passed"},
                ensure_ascii=False), flush=True)
            if live:
                assert settings.openai_api_key
    finally:
        server.terminate()
        try:
            await asyncio.wait_for(server.wait(), 10)
        except TimeoutError:
            server.kill()
            await asyncio.wait_for(server.wait(), 5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Makes paid OpenAI API calls")
    args = parser.parse_args()
    os.environ["MOCK_MODE"] = str(not args.live).lower()
    os.environ["EMBEDDINGS_ENABLED"] = str(args.live).lower()
    if args.live and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = getpass.getpass("OpenAI API key (not saved): ")
    asyncio.run(run(args.live))


if __name__ == "__main__":
    main()
