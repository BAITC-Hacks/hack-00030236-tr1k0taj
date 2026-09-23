import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.kernel.context import history, snapshot
from app.kernel.runtime import Runtime
from app.kernel.store import KernelError
from app.kernel.types import (
    CreateSession,
    InterruptRequest,
    PlaybackRequest,
    SessionSnapshot,
    TurnAccepted,
    TurnRequest,
)

router = APIRouter(tags=["agent-kernel"])


def runtime(request: Request) -> Runtime:
    return request.app.state.kernel


Kernel = Annotated[Runtime, Depends(runtime)]


@router.get("/kernel/capabilities")
async def capabilities():
    return {
        "protocol_version": 1, "mode": "mock" if settings.mock_mode else "live",
        "streaming": "sse", "playback": "segment-timeline-v1",
        "llm_configured": bool(settings.openai_api_key),
        "embeddings_configured": settings.embeddings_enabled and bool(settings.openai_api_key),
        "rag": True, "audio": False, "single_worker": True,
        "max_agents": 8, "max_segments": settings.kernel_max_segments,
        "max_turns": 100, "max_text_chars": 8000,
    }


@router.post("/sessions", response_model=SessionSnapshot, status_code=201)
async def create(request: CreateSession, kernel: Kernel):
    return await kernel.create(request)


@router.get("/sessions/{session_id}", response_model=SessionSnapshot)
async def get_session(session_id: UUID, kernel: Kernel):
    return snapshot(await kernel.repo.get(str(session_id)))


@router.post("/sessions/{session_id}/turns", response_model=TurnAccepted, status_code=202)
async def turn(session_id: UUID, request: TurnRequest, kernel: Kernel):
    return await kernel.submit(str(session_id), request)


@router.post("/sessions/{session_id}/interrupt")
async def interrupt(session_id: UUID, request: InterruptRequest, kernel: Kernel):
    return await kernel.playback(str(session_id), request, interrupt=True)


@router.post("/sessions/{session_id}/playback")
async def playback(session_id: UUID, request: PlaybackRequest, kernel: Kernel):
    return await kernel.playback(str(session_id), request)


@router.get("/sessions/{session_id}/history")
async def get_history(session_id: UUID, kernel: Kernel):
    return {"session_id": str(session_id), "messages": history(await kernel.repo.get(str(session_id)))}


@router.get("/sessions/{session_id}/trace")
async def trace(session_id: UUID, kernel: Kernel):
    state = await kernel.repo.get(str(session_id))
    return {
        "session_id": str(session_id), "last_seq": state["last_seq"],
        "agents": [{key: run[key] for key in (
            "run_id", "agent_id", "turn_id", "generation", "input_revision", "status"
        )} for run in state["runs"].values()],
        "responses": [{"response_id": response["response_id"], "status": response["status"],
            "segments": [{key: segment[key] for key in (
                "segment_id", "status", "used_source_ids", "consumed_board_seq"
            )} for segment in response["segments"]]} for response in state["responses"].values()],
    }


@router.post("/sessions/{session_id}/close")
async def close(session_id: UUID, kernel: Kernel):
    return await kernel.close_session(str(session_id))


@router.get("/sessions/{session_id}/events")
async def events(
    session_id: UUID, kernel: Kernel, request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
):
    sid = str(session_id)
    state = await kernel.repo.get(sid)
    if last_event_id is not None:
        try:
            after = max(after, int(last_event_id))
        except ValueError as exc:
            raise KernelError("invalid_cursor", "Last-Event-ID должен быть числом", 422) from exc
    if after < 0 or after > state["last_seq"]:
        raise KernelError("invalid_cursor", "Курсор вне границ журнала сессии", 422)

    async def stream():
        cursor = after
        closed_seen = False
        while not await request.is_disconnected():
            # Clear before reading DB: a concurrent commit either appears in the read or sets the flag.
            kernel.signals[sid].clear()
            batch = await kernel.repo.events(sid, cursor)
            if not batch and closed_seen:
                return
            for item in batch:
                cursor = item["seq"]
                yield f"id: {cursor}\nevent: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            if len(batch) == 200:
                continue
            state = await kernel.repo.get(sid)
            if state["status"] == "closed":
                # Closing can race the query; drain until the public session.closed event was delivered.
                if batch and batch[-1]["type"] == "session.closed":
                    return
                closed_seen = True
                continue
            try:
                await asyncio.wait_for(kernel.signals[sid].wait(), timeout=1)
            except TimeoutError:
                yield ": keep-alive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
