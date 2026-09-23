from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.catalog import Catalog
from app.knowledge.loader import load_kit
from app.knowledge.records import Records
from app.knowledge.search import Search
from app.knowledge.store import Store


class Knowledge:
    """Single entry point for the agent kernel: Knowledge(session).catalog / .records / .search."""

    def __init__(self, session: AsyncSession):
        self.store = Store(session)
        self.catalog = Catalog(self.store)
        self.records = Records(self.store)
        self.search = Search(self.store)

    async def reload(self, *, reset: bool = True) -> int:
        from app.config import settings

        return await load_kit(self.store.session, Path(settings.datasets_dir), reset=reset)


@asynccontextmanager
async def open_knowledge() -> AsyncIterator[Knowledge]:
    """For kernel code outside a request (background assistant, eval): owns its own session."""
    from app.db import SessionLocal

    async with SessionLocal() as session:
        yield Knowledge(session)
