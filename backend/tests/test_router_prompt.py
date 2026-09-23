import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.knowledge import Knowledge, ensure_loaded
from app.router import OpenAIRouter


def test_prompt_has_full_catalog_and_verbatim_not_this_if():
    async def run():
        engine = create_async_engine(settings.database_url)
        try:
            async with async_sessionmaker(engine)() as db:
                await ensure_loaded(db, Path(settings.datasets_dir))
                kb = Knowledge(db)
                prompt, ids = await OpenAIRouter(client=object()).system_prompt(kb)
                scenarios = await kb.catalog.scenarios()
        finally:
            await engine.dispose()
        assert len(scenarios) == 40
        assert set(ids) == {s.scenario_id for s in scenarios} | {
            "SYS_OUT_OF_SCOPE", "SYS_UNCLEAR", "SYS_GOODBYE"}
        for i in ids:
            assert i in prompt
        for s in scenarios:
            for n in s.not_this_if:
                assert f"not_this_if: {n.condition} -> {n.use_instead}" in prompt

    asyncio.run(run())
