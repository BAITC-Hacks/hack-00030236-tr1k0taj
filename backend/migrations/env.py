import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Models imported so autogenerate sees them. Add new model modules here.
import app.context.models
import app.kernel.models
import app.knowledge.models  # noqa: F401
from app.config import settings
from app.db import Base

target_metadata = Base.metadata


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


asyncio.run(run_migrations_online())
