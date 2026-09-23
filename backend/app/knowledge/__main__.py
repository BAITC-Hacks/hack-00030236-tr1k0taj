"""python -m app.knowledge [--reset] — (re)load the starter kit into Postgres."""

import asyncio
import sys
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.knowledge.loader import load_kit


async def main(reset: bool) -> None:
    async with SessionLocal() as session:
        n = await load_kit(session, Path(settings.datasets_dir), reset=reset)
    print(f"loaded {n} kit records from {settings.datasets_dir} (reset={reset})")


asyncio.run(main(reset="--reset" in sys.argv))
