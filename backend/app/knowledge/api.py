"""Small HTTP CRUD over kit_records for UI and debugging. Edits live in the DB only (origin=user);
kit files stay untouched and POST /kit/reload?reset=true restores the clean kit."""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.knowledge.service import Knowledge
from app.knowledge.types import KEY_FIELDS, KIND_MODELS, Hit, Scenario

router = APIRouter(prefix="/kit", tags=["kit"])


async def _knowledge(session: Annotated[AsyncSession, Depends(get_session)]) -> Knowledge:
    return Knowledge(session)


KB = Annotated[Knowledge, Depends(_knowledge)]


@router.get("")
async def kinds(kb: KB) -> dict[str, int]:
    return await kb.store.kinds()


@router.get("/search")
async def search(
    kb: KB,
    q: str,
    kind: Annotated[list[str] | None, Query()] = None,
    limit: int = 5,
) -> list[Hit]:
    return await kb.search.query(q, kinds=kind, limit=limit)


@router.post("/reload")
async def reload(kb: KB, reset: bool = True) -> dict[str, int]:
    return {"loaded": await kb.reload(reset=reset)}


@router.get("/{kind}")
async def list_records(kb: KB, kind: str) -> list[Any]:
    return await kb.store.all(kind)


@router.get("/{kind}/{key}")
async def get_record(kb: KB, kind: str, key: str) -> Any:
    payload = await kb.store.get(kind, key)
    if payload is None:
        raise HTTPException(404, f"{kind}/{key} not found")
    return payload


@router.put("/{kind}/{key}")
async def put_record(kb: KB, kind: str, key: str, payload: Annotated[Any, Body()]) -> Any:
    if model := KIND_MODELS.get(kind):
        try:
            obj = model.model_validate(payload)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False)) from e
        if getattr(obj, KEY_FIELDS[kind]) != key:
            raise HTTPException(422, f"{KEY_FIELDS[kind]} must equal '{key}'")
        if isinstance(obj, Scenario) and (errors := await kb.catalog.validate_scenario(obj)):
            raise HTTPException(422, errors)
    await kb.store.put(kind, key, payload)
    return payload


@router.delete("/{kind}/{key}", status_code=204)
async def delete_record(kb: KB, kind: str, key: str) -> None:
    if not await kb.store.delete(kind, key):
        raise HTTPException(404, f"{kind}/{key} not found")
