"""python -m app.knowledge load [--reset]   — (re)load the starter kit into Postgres
python -m app.knowledge expand [--force]  — regenerate expansions.json with an LLM (needs a key)
"""

import asyncio
import sys
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.knowledge.expansions import EXPANSIONS_PATH, generate
from app.knowledge.loader import build_records, load_kit


async def load(reset: bool) -> None:
    async with SessionLocal() as session:
        n = await load_kit(session, Path(settings.datasets_dir), reset=reset)
    print(f"loaded {n} kit records from {settings.datasets_dir} (reset={reset})")


async def expand(force: bool) -> None:
    if not settings.openai_api_key:
        sys.exit("OPENAI_API_KEY is required to generate expansions")
    new, kept = await generate(
        build_records(Path(settings.datasets_dir)),
        settings.openai_api_key,
        settings.expansion_model,
        Path(settings.datasets_dir),
        force=force,
    )
    print(f"expansions: {new} generated, {kept} unchanged -> {EXPANSIONS_PATH}")


command = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "load"
if command == "expand":
    asyncio.run(expand(force="--force" in sys.argv))
else:
    asyncio.run(load(reset="--reset" in sys.argv))
