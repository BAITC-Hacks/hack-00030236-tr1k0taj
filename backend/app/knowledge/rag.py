"""Document-only pgvector retrieval. Database transactions never span embedding requests."""

import asyncio
import json
import math
from collections import defaultdict
from typing import Any

from openai import APIError, AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import settings

RAG_KINDS = ("kb", "office", "clinic", "inspection_point")
MAX_QUERY_CHARS = 4000
MAX_DOCUMENT_CHARS = 16000

_VALID_VECTOR = """
    e.kind = k.kind AND e.key = k.key AND e.model = :model
    AND e.dimensions = :dimensions AND e.text_hash = md5(k.search_text)
"""
_COVERAGE = text(
    f"""SELECT count(*) AS total, count(e.key) AS indexed
        FROM kit_records k LEFT JOIN knowledge_vectors e ON {_VALID_VECTOR}
        WHERE k.kind = ANY(:kinds)"""
)
_PENDING = text(
    f"""SELECT k.kind, k.key, k.search_text, md5(k.search_text) AS text_hash
        FROM kit_records k LEFT JOIN knowledge_vectors e ON {_VALID_VECTOR}
        WHERE k.kind = ANY(:kinds) AND e.key IS NULL
        ORDER BY k.kind, k.key"""
)
_SEMANTIC = text(
    f"""SELECT k.kind, k.key, k.payload, left(k.search_text, 240) AS snippet
        FROM kit_records k JOIN knowledge_vectors e ON {_VALID_VECTOR}
        WHERE k.kind = ANY(:kinds)
        ORDER BY e.embedding <=> CAST(:embedding AS vector), k.kind, k.key
        LIMIT :limit"""
)
_UPSERT = text(
    """INSERT INTO knowledge_vectors (kind, key, model, dimensions, text_hash, embedding)
       SELECT kind, key, :model, :dimensions, CAST(:text_hash AS text), CAST(:embedding AS vector)
       FROM kit_records
       WHERE kind = :kind AND key = :key AND md5(search_text) = :text_hash
       ON CONFLICT (kind, key) DO UPDATE SET
           model = EXCLUDED.model, dimensions = EXCLUDED.dimensions,
           text_hash = EXCLUDED.text_hash, embedding = EXCLUDED.embedding,
           updated_at = now()"""
)


def document_kinds(kinds: list[str] | None) -> list[str]:
    selected = list(RAG_KINDS) if kinds is None else list(dict.fromkeys(kinds))
    if not selected or any(kind not in RAG_KINDS for kind in selected):
        raise ValueError("RAG kinds must be kb, office, clinic or inspection_point")
    return selected


def embedding_unavailable() -> str | None:
    if not settings.embeddings_enabled:
        return "embeddings_disabled"
    if not settings.openai_api_key:
        return "embedding_key_missing"
    return None


def _parameters(kinds: list[str]) -> dict[str, Any]:
    return {
        "kinds": kinds,
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
    }


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """The only external call; callers close their DB session before reaching this."""
    async with AsyncOpenAI(
        api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds, max_retries=0
    ) as client:
        response = await client.embeddings.create(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            input=texts,
            encoding_format="float",
        )
    items = sorted(response.data, key=lambda item: item.index)
    if [item.index for item in items] != list(range(len(texts))):
        raise ValueError("Invalid embedding response")
    vectors = [item.embedding for item in items]
    for vector in vectors:
        if (
            len(vector) != settings.embedding_dimensions
            or not all(math.isfinite(value) for value in vector)
            or not any(vector)
        ):
            raise ValueError("Invalid embedding vector")
    return vectors


def fuse_ranks(lexical: list[dict], semantic: list[dict], limit: int) -> list[dict]:
    """Reciprocal rank fusion; scores are rank weights, not factual confidence."""
    scores: dict[tuple[str, str], float] = defaultdict(float)
    documents: dict[tuple[str, str], dict] = {}
    for ranking in (lexical, semantic):
        for rank, hit in enumerate(ranking, start=1):
            identity = (hit["kind"], hit["key"])
            scores[identity] += 1 / (60 + rank)
            documents[identity] = hit
    identities = sorted(scores, key=lambda identity: (-scores[identity], identity))[:limit]
    return [{**documents[key], "score": round(scores[key], 6)} for key in identities]


def sourced(hit: dict) -> dict:
    return {**hit, "source": "knowledge", "source_id": f"{hit['kind']}:{hit['key']}"}


class RagIndex:
    def __init__(self, bind: AsyncEngine):
        # Independent short sessions also allow a cancelled provider call to release every DB slot.
        self.sessions = async_sessionmaker(bind, expire_on_commit=False)

    async def query(self, q: str, kinds: list[str] | None, limit: int) -> dict:
        from app.knowledge.search import Search
        from app.knowledge.store import Store

        started = asyncio.get_running_loop().time()
        kinds = document_kinds(kinds)
        q = q.strip()
        if not q or len(q) > MAX_QUERY_CHARS or not 1 <= limit <= 20:
            raise ValueError("RAG query requires 1..4000 characters and limit 1..20")
        params = _parameters(kinds)
        # Snapshot lexical results and index status, then release the connection before HTTP.
        async with self.sessions() as session:
            lexical = [
                hit.model_dump()
                for hit in await Search(Store(session)).query(q, kinds, max(20, limit * 4))
            ]
            reason = embedding_unavailable()
            if reason is None:
                try:
                    coverage = (await session.execute(_COVERAGE, params)).one()
                    if not coverage.total:
                        reason = "index_empty"
                    elif coverage.indexed != coverage.total:
                        reason = "index_incomplete"
                except SQLAlchemyError:
                    reason = "index_unavailable"
        fallback = {"hits": [sourced(h) for h in lexical[:limit]], "search_mode": "lexical"}
        if reason:
            return {**fallback, "degraded_reason": reason}
        try:
            # The outer tool has llm_timeout_seconds for the entire operation. Reserve time
            # for returning lexical evidence, including when the provider never responds.
            elapsed = asyncio.get_running_loop().time() - started
            budget = max(0, settings.llm_timeout_seconds * 0.75 - elapsed)
            async with asyncio.timeout(budget):
                vector = (await embed_texts([q]))[0]
        except (APIError, ValueError, TimeoutError):
            return {**fallback, "degraded_reason": "embedding_request_failed"}
        try:
            async with self.sessions() as session:
                # A single repeatable snapshot checks changes made while the provider was running.
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                coverage = (await session.execute(_COVERAGE, params)).one()
                if not coverage.total or coverage.indexed != coverage.total:
                    current = await Search(Store(session)).query(q, kinds, limit)
                    return {
                        "hits": [sourced(hit.model_dump()) for hit in current],
                        "search_mode": "lexical",
                        "degraded_reason": "index_changed",
                    }
                # Fetch lexical again so payloads and both rankings use the same DB snapshot.
                lexical = [
                    hit.model_dump()
                    for hit in await Search(Store(session)).query(q, kinds, max(20, limit * 4))
                ]
                semantic = (
                    await session.execute(
                        _SEMANTIC,
                        {**params, "embedding": json.dumps(vector), "limit": max(20, limit * 4)},
                    )
                ).mappings().all()
                hits = fuse_ranks(lexical, [dict(row) for row in semantic], limit)
            return {"hits": [sourced(hit) for hit in hits], "search_mode": "hybrid"}
        except SQLAlchemyError:
            return {**fallback, "degraded_reason": "index_unavailable"}

    async def reindex(self, batch_size: int = 64) -> dict:
        if not 1 <= batch_size <= 128:
            raise ValueError("batch_size must be 1..128")
        result = {"indexed": 0, "skipped": 0, "status": "complete"}
        if reason := embedding_unavailable():
            return {**result, "status": "disabled", "degraded_reason": reason}
        params = _parameters(list(RAG_KINDS))
        try:
            async with self.sessions() as session:
                pending = (await session.execute(_PENDING, params)).mappings().all()
            for start in range(0, len(pending), batch_size):
                batch = pending[start : start + batch_size]
                texts = [f"{row['kind']}: {row['key']}\n{row['search_text']}" for row in batch]
                # Do not silently truncate public CRUD documents or pass provider-sized surprises.
                safe = [
                    (row, value)
                    for row, value in zip(batch, texts, strict=True)
                    if len(value) <= MAX_DOCUMENT_CHARS
                ]
                result["skipped"] += len(batch) - len(safe)
                if not safe:
                    continue
                try:
                    vectors = await embed_texts([value for _, value in safe])
                except (APIError, ValueError, TimeoutError):
                    return {
                        **result,
                        "status": "degraded",
                        "degraded_reason": "embedding_request_failed",
                    }
                batch_indexed = 0
                async with self.sessions.begin() as session:
                    for (row, _), vector in zip(safe, vectors, strict=True):
                        written = await session.execute(
                            _UPSERT,
                            {**params, **dict(row), "embedding": json.dumps(vector)},
                        )
                        batch_indexed += written.rowcount
                result["indexed"] += batch_indexed
                result["skipped"] += len(safe) - batch_indexed
            if result["skipped"]:
                return {**result, "status": "degraded", "degraded_reason": "index_incomplete"}
            return result
        except SQLAlchemyError:
            return {**result, "status": "degraded", "degraded_reason": "index_unavailable"}


# Coalesce CRUD bursts into one indexer per engine/event loop. Never hold the caller's session.
_index_tasks: dict[tuple, tuple[asyncio.Task, asyncio.Event]] = {}


def schedule_reindex(bind: AsyncEngine) -> asyncio.Task | None:
    if embedding_unavailable():
        return None
    loop = asyncio.get_running_loop()
    key = (loop, bind)
    current = _index_tasks.get(key)
    if current and not current[0].done():
        current[1].set()
        return current[0]
    dirty = asyncio.Event()
    dirty.set()

    async def run() -> None:
        try:
            while dirty.is_set():
                dirty.clear()
                await RagIndex(bind).reindex()
        finally:
            _index_tasks.pop(key, None)

    task = loop.create_task(run(), name="knowledge-reindex")
    _index_tasks[key] = (task, dirty)
    return task


async def reindex_knowledge(batch_size: int = 64) -> dict:
    from app.db import engine

    return await RagIndex(engine).reindex(batch_size)


def schedule_reindex_knowledge() -> asyncio.Task | None:
    """Startup entry point, coalesced with indexing already requested by the kit loader."""
    from app.db import engine

    return schedule_reindex(engine)


async def stop_reindex_knowledge() -> None:
    """Lifespan shutdown: finish cancellation before the application's engine is disposed."""
    from app.db import engine

    current = _index_tasks.get((asyncio.get_running_loop(), engine))
    if current:
        current[0].cancel()
        await asyncio.gather(current[0], return_exceptions=True)
