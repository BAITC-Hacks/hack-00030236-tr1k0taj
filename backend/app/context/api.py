from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.context.service import Contexts
from app.context.types import BoardEntry, SessionContext, SessionNotFound


def get_contexts(request: Request) -> Contexts:
    return request.app.state.contexts


Ctx = Annotated[Contexts, Depends(get_contexts)]
router = APIRouter(prefix="/sessions", tags=["context"])


@router.get("/{session_id}/context")
async def get_context(session_id: str, ctx: Ctx) -> SessionContext:
    try:
        return await ctx.snapshot(session_id)
    except SessionNotFound:
        raise HTTPException(404, "session not found") from None


@router.get("/{session_id}/board")
async def get_board(session_id: str, ctx: Ctx, since_turn: int | None = None) -> list[BoardEntry]:
    return await ctx.board(session_id, since_turn)
