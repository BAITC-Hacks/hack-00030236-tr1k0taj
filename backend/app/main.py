from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, engine, get_session
from app.knowledge import ensure_loaded
from app.knowledge.api import router as kit_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with SessionLocal() as session:
        await ensure_loaded(session, Path(settings.datasets_dir))
    yield
    await engine.dispose()


app = FastAPI(title="Voice Router API", lifespan=lifespan)
app.include_router(kit_router)

Session = Annotated[AsyncSession, Depends(get_session)]


class Health(BaseModel):
    status: str
    db: str


@app.get("/health")
async def health(session: Session) -> Health:
    await session.execute(text("SELECT 1"))
    return Health(status="ok", db="ok")
