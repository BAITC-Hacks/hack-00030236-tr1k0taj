from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, engine, get_session
from app.kernel.api import router as kernel_router
from app.kernel.provider import ModelDriver
from app.kernel.runtime import Runtime
from app.kernel.store import KernelError
from app.knowledge import ensure_loaded, schedule_reindex_knowledge, stop_reindex_knowledge
from app.knowledge.api import router as kit_router


@asynccontextmanager
async def lifespan(application: FastAPI):
    async with SessionLocal() as session:
        await ensure_loaded(session, Path(settings.datasets_dir))
    kernel = Runtime(ModelDriver(api_key=settings.openai_api_key, model=settings.llm_model,
                                 mock=settings.mock_mode, timeout=settings.llm_timeout_seconds))
    application.state.kernel = kernel
    await kernel.recover()
    schedule_reindex_knowledge()
    try:
        yield
    finally:
        await kernel.shutdown()
        await stop_reindex_knowledge()
        await engine.dispose()


app = FastAPI(title="Voice Router API", lifespan=lifespan)
app.include_router(kit_router)
app.include_router(kernel_router)


@app.exception_handler(KernelError)
async def kernel_error(_: Request, exc: KernelError):
    return JSONResponse(status_code=exc.status, content={"error": {
        "code": exc.code, "message": exc.message,
    }})

Session = Annotated[AsyncSession, Depends(get_session)]


class Health(BaseModel):
    status: str
    db: str


@app.get("/health")
async def health(session: Session) -> Health:
    await session.execute(text("SELECT 1"))
    return Health(status="ok", db="ok")
