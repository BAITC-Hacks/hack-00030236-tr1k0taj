import asyncio
import json

from app.db import engine
from app.knowledge import reindex_knowledge


async def main():
    try:
        print(json.dumps(await reindex_knowledge()))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
