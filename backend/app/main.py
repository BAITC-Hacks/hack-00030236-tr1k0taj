from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import call, context, kernel, knowledge, tracer
from app.config import settings
from app.db import SessionLocal, engine, get_session
from app.docs import DESCRIPTION, TAGS
from app.kernel import KernelError, ModelDriver, Repository, Runtime
from app.knowledge import ensure_loaded, schedule_reindex_knowledge, stop_reindex_knowledge

# до создания app: span'ы middleware и lifespan пишутся в настроенный провайдер (ADR 0013)
tracer.setup_tracing()


@asynccontextmanager
async def lifespan(application: FastAPI):
    async with SessionLocal() as session:
        await ensure_loaded(session, Path(settings.datasets_dir))
    contexts = context.Contexts(context.PgStore())
    kernel = Runtime(ModelDriver(api_key=settings.openai_api_key, model=settings.llm_model,
                                 mock=settings.mock_mode, timeout=settings.llm_timeout_seconds),
                     repository=Repository(contexts=contexts))
    application.state.contexts = contexts
    application.state.kernel = kernel
    application.state.calls = call.CallService(contexts, call.build_providers(settings), kernel=kernel)
    await kernel.recover()
    schedule_reindex_knowledge()
    try:
        yield
    finally:
        await kernel.shutdown()
        await stop_reindex_knowledge()
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
app.include_router(knowledge.api_router)
app.include_router(kernel.api_router)
app.include_router(tracer.api_router)
# последним add_middleware → самый внешний: SERVER span покрывает весь запрос и SSE-тело
app.add_middleware(tracer.TraceMiddleware)


@app.exception_handler(KernelError)
async def kernel_error(_: Request, exc: KernelError):
    return JSONResponse(status_code=exc.status, content={"error": {
        "code": exc.code, "message": exc.message,
    }})

Session = Annotated[AsyncSession, Depends(get_session)]


class Health(BaseModel):
    status: str
    db: str


@app.get("/health", tags=["health"], summary="Backend и БД живы")
async def health(session: Session) -> Health:
    await session.execute(text("SELECT 1"))
    return Health(status="ok", db="ok")
