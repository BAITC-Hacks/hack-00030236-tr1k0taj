from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import call, context
from app.config import settings
from app.db import SessionLocal, engine, get_session
from app.docs import DESCRIPTION, TAGS
from app.knowledge import ensure_loaded
from app.knowledge.api import router as kit_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with SessionLocal() as session:
        await ensure_loaded(session, Path(settings.datasets_dir))
    app.state.contexts = context.Contexts(context.PgStore())
    app.state.calls = call.CallService(app.state.contexts, call.build_providers(settings))
    yield
    await engine.dispose()


app = FastAPI(
    title="Voice Router API",
    version="0.1.0",
    summary="Голосовой роутер сценариев страховой Saqta Insurance",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    lifespan=lifespan,
)
app.include_router(call.api_router)
app.include_router(context.api_router)
app.include_router(kit_router)

Session = Annotated[AsyncSession, Depends(get_session)]


class Health(BaseModel):
    status: str
    db: str


@app.get("/health", tags=["health"], summary="Backend и БД живы")
async def health(session: Session) -> Health:
    await session.execute(text("SELECT 1"))
    return Health(status="ok", db="ok")
