"""Document-only pgvector retrieval. Database transactions never span embedding requests."""

import asyncio
import json
import math
import time
from collections import OrderedDict, defaultdict
from typing import Any

from openai import APIError, AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import settings
from app.knowledge.expansions import load_expansions

RAG_KINDS = ("kb", "office", "clinic", "inspection_point")
MAX_QUERY_CHARS = 4000
MAX_DOCUMENT_CHARS = 16000

_VALID_VECTOR = """
    e.kind = k.kind AND e.key = k.key AND e.model = :model
    AND e.dimensions = :dimensions AND e.text_hash = md5(k.search_text)
"""
# A record has several vectors (idx 0 = original text, 1.. = ru/kk/mixed questions); coverage
# and pending are counted per record, similarity of a record = its best variant (ADR 0010).
_COVERAGE = text(
    f"""SELECT count(DISTINCT k.kind || ':' || k.key) AS total,
               count(DISTINCT e.kind || ':' || e.key) AS indexed
        FROM kit_records k LEFT JOIN knowledge_vectors e ON {_VALID_VECTOR}
        WHERE k.kind = ANY(:kinds)"""
)
_PENDING = text(
    f"""SELECT DISTINCT k.kind, k.key, k.payload, k.search_text, md5(k.search_text) AS text_hash
        FROM kit_records k LEFT JOIN knowledge_vectors e ON {_VALID_VECTOR}
        WHERE k.kind = ANY(:kinds) AND e.key IS NULL
        ORDER BY k.kind, k.key"""
)
# With search_query: similarity = RAW_WEIGHT * sim(client text) + (1 - RAW_WEIGHT) * sim(English)
RAW_WEIGHT = 0.3
# Lexical ranking weight in RRF (semantic = 1.0). Tuned on `just eval-search`.
LEXICAL_RRF_WEIGHT = 0.3
_SEMANTIC = text(
    f"""SELECT k.kind, k.key, k.payload, left(k.search_text, 240) AS snippet,
               :w_raw * max(1 - (e.embedding <=> CAST(:embedding AS vector)))
               + (1 - :w_raw) * max(1 - (e.embedding <=> CAST(:embedding_en AS vector)))
               AS similarity
        FROM kit_records k JOIN knowledge_vectors e ON {_VALID_VECTOR}
        WHERE k.kind = ANY(:kinds)
        GROUP BY k.kind, k.key
        ORDER BY similarity DESC, k.kind, k.key
        LIMIT :limit"""
)
_UPSERT = text(
    """INSERT INTO knowledge_vectors (kind, key, idx, model, dimensions, text_hash, embedding)
       SELECT kind, key, :idx, :model, :dimensions, CAST(:text_hash AS text),
              CAST(:embedding AS vector)
       FROM kit_records
       WHERE kind = :kind AND key = :key AND md5(search_text) = :text_hash
       ON CONFLICT (kind, key, idx) DO UPDATE SET
           model = EXCLUDED.model, dimensions = EXCLUDED.dimensions,
           text_hash = EXCLUDED.text_hash, embedding = EXCLUDED.embedding,
           updated_at = now()"""
)
_DROP_EXTRA_VARIANTS = text(
    "DELETE FROM knowledge_vectors WHERE kind = :kind AND key = :key AND idx >= :count"
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


# Process-local caches (hackathon perf, ADR none). The knowledge base is read-only at runtime,
# so cached query embeddings and rag_query results are safe as long as they are dropped whenever
# the kit is (re)loaded — see load_kit(), the only place kit_records changes.
_EMBED_CACHE_SIZE = 256
_embed_cache: OrderedDict[tuple[str, str, int], list[float]] = OrderedDict()
_query_cache: dict[tuple[str, str | None, tuple[str, ...], int], tuple[float, dict]] = {}


def clear_knowledge_cache() -> None:
    """Drop the query-embedding LRU and the rag_query TTL cache. Call after the kit changes."""
    _embed_cache.clear()
    _query_cache.clear()


async def _embed_query_cached(texts: list[str]) -> list[list[float]]:
    """LRU (~256) over embed_texts for query text, keyed by (text, model, dimensions)."""
    keys = [(t, settings.embedding_model, settings.embedding_dimensions) for t in texts]
    results: list[list[float] | None] = []
    for key in keys:
        vector = _embed_cache.get(key)
        if vector is not None:
            _embed_cache.move_to_end(key)
        results.append(vector)
    missing = [i for i, vector in enumerate(results) if vector is None]
    if missing:
        fetched = await embed_texts([texts[i] for i in missing])
        for i, vector in zip(missing, fetched, strict=True):
            _embed_cache[keys[i]] = vector
            _embed_cache.move_to_end(keys[i])
            results[i] = vector
        while len(_embed_cache) > _EMBED_CACHE_SIZE:
            _embed_cache.popitem(last=False)
    return results  # type: ignore[return-value]


def _query_cache_get(key: tuple[str, str | None, tuple[str, ...], int]) -> dict | None:
    if settings.knowledge_cache_ttl <= 0:
        return None
    entry = _query_cache.get(key)
    if entry is None:
        return None
    expires_at, result = entry
    if expires_at < time.monotonic():
        _query_cache.pop(key, None)
        return None
    return result


def _query_cache_put(key: tuple[str, str | None, tuple[str, ...], int], result: dict) -> None:
    if settings.knowledge_cache_ttl <= 0:
        return
    _query_cache[key] = (time.monotonic() + settings.knowledge_cache_ttl, result)


def fuse_ranks(
    lexical: list[dict],
    semantic: list[dict],
    limit: int,
    *,
    lexical_weight: float = 1.0,
) -> list[dict]:
    """Reciprocal rank fusion; scores are rank weights, not factual confidence."""
    scores: dict[tuple[str, str], float] = defaultdict(float)
    documents: dict[tuple[str, str], dict] = {}
    for ranking, weight in ((lexical, lexical_weight), (semantic, 1.0)):
        for rank, hit in enumerate(ranking, start=1):
            identity = (hit["kind"], hit["key"])
            scores[identity] += weight / (60 + rank)
            documents[identity] = hit
    identities = sorted(scores, key=lambda identity: (-scores[identity], identity))[:limit]
    return [{**documents[key], "score": round(scores[key], 6)} for key in identities]


def _record_texts(row, expansions: dict[str, dict]) -> list[str]:
    """Texts to embed for one record: its document text first, then fresh ru/kk/mixed
    expansion questions. Records without an expansion keep the single original vector."""
    from app.knowledge.loader import document_variants

    variants = document_variants(row["kind"], row["key"], row["payload"], expansions)
    if len(variants) == 1:
        return [f"{row['kind']}: {row['key']}\n{row['search_text']}"]
    # Questions stay unprefixed: an English "kb: key" header hurts Kazakh matching
    # (eval: kk recall@3 1.00 -> 0.76 with the prefix).
    return [f"{row['kind']}: {row['key']}\n{variants[0][1]}"] + [t for _, t in variants[1:]]


def _record_batches(records: list, batch_size: int):
    """Group whole records so one batch holds about batch_size texts (at least one record)."""
    batch, size = [], 0
    for record in records:
        if batch and size + len(record[1]) > batch_size:
            yield batch
            batch, size = [], 0
        batch.append(record)
        size += len(record[1])
    if batch:
        yield batch


def sourced(hit: dict) -> dict:
    return {**hit, "source": "knowledge", "source_id": f"{hit['kind']}:{hit['key']}"}


class RagIndex:
    def __init__(self, bind: AsyncEngine):
        # Independent short sessions also allow a cancelled provider call to release every DB slot.
        self.sessions = async_sessionmaker(bind, expire_on_commit=False)

    async def query(
        self,
        q: str,
        kinds: list[str] | None,
        limit: int,
        *,
        search_query: str | None = None,
    ) -> dict:
        from app.knowledge.search import Search
        from app.knowledge.store import Store

        started = asyncio.get_running_loop().time()
        kinds = document_kinds(kinds)
        q = q.strip()
        # search_query: English rewrite from the router (same LLM call). Searched together with
        # the client's own words; alone it loses nuance, the raw text alone misses Kazakh.
        search_query = (search_query or "").strip() or None
        if search_query and len(search_query) > MAX_QUERY_CHARS:
            raise ValueError("Invalid semantic search query")
        if not q or len(q) > MAX_QUERY_CHARS or not 1 <= limit <= 20:
            raise ValueError("RAG query requires 1..4000 characters and limit 1..20")
        cache_key = (q, search_query, tuple(kinds), limit)
        cached = _query_cache_get(cache_key)
        if cached is not None:
            return cached
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
                vectors = await _embed_query_cached([q, search_query] if search_query else [q])
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
                    (
                        await session.execute(
                            _SEMANTIC,
                            {
                                **params,
                                "embedding": json.dumps(vectors[0]),
                                "embedding_en": json.dumps(vectors[-1]),
                                "w_raw": RAW_WEIGHT if search_query else 1.0,
                                "limit": max(20, limit * 4),
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
                hits = fuse_ranks(
                    lexical,
                    [dict(row) for row in semantic],
                    limit,
                    lexical_weight=LEXICAL_RRF_WEIGHT,
                )
            result = {"hits": [sourced(hit) for hit in hits], "search_mode": "hybrid"}
            _query_cache_put(cache_key, result)
            return result
        except SQLAlchemyError:
            return {**fallback, "degraded_reason": "index_unavailable"}

    async def reindex(self, batch_size: int = 64) -> dict:
        if not 1 <= batch_size <= 128:
            raise ValueError("batch_size must be 1..128")
        result = {"indexed": 0, "skipped": 0, "status": "complete"}
        if reason := embedding_unavailable():
            return {**result, "status": "disabled", "degraded_reason": reason}
        params = _parameters(list(RAG_KINDS))
        expansions = load_expansions()
        try:
            async with self.sessions() as session:
                pending = (await session.execute(_PENDING, params)).mappings().all()
            # Do not silently truncate public CRUD documents or pass provider-sized surprises.
            records = []
            for row in pending:
                texts = _record_texts(row, expansions)
                if all(len(t) <= MAX_DOCUMENT_CHARS for t in texts):
                    records.append((row, texts))
                else:
                    result["skipped"] += 1
            for batch in _record_batches(records, batch_size):
                flat = [t for _, texts in batch for t in texts]
                try:
                    vectors = []
                    for start in range(0, len(flat), batch_size):
                        vectors += await embed_texts(flat[start : start + batch_size])
                except (APIError, ValueError, TimeoutError):
                    return {
                        **result,
                        "status": "degraded",
                        "degraded_reason": "embedding_request_failed",
                    }
                batch_indexed = 0
                offset = 0
                # All variants of a record land in one transaction, so a record is either fully
                # indexed or not at all (coverage counts records).
                async with self.sessions.begin() as session:
                    for row, texts in batch:
                        written = 0
                        for idx, vector in enumerate(vectors[offset : offset + len(texts)]):
                            res = await session.execute(
                                _UPSERT,
                                {
                                    **params,
                                    **dict(row),
                                    "idx": idx,
                                    "embedding": json.dumps(vector),
                                },
                            )
                            written += res.rowcount
                        await session.execute(
                            _DROP_EXTRA_VARIANTS,
                            {"kind": row["kind"], "key": row["key"], "count": len(texts)},
                        )
                        offset += len(texts)
                        batch_indexed += written == len(texts)
                result["indexed"] += batch_indexed
                result["skipped"] += len(batch) - batch_indexed
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
