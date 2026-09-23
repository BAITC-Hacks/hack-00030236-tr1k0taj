from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session

app = FastAPI(title="Voice Router API")

Session = Annotated[AsyncSession, Depends(get_session)]


class Health(BaseModel):
    status: str
    db: str


@app.get("/health")
async def health(session: Session) -> Health:
    await session.execute(text("SELECT 1"))
    return Health(status="ok", db="ok")
