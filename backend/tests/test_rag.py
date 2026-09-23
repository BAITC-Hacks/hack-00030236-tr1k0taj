"""Real pgvector/FTS integration; embedding HTTP is always replaced with deterministic vectors."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.knowledge import Knowledge, load_kit, rag


@pytest.fixture
def embeddings(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "unit-test-key")
    monkeypatch.setattr(settings, "embedding_model", "test-embedding-model")
    monkeypatch.setattr(settings, "embedding_dimensions", 3)
    # Index explicitly so background tasks do not compete with transaction assertions.
    monkeypatch.setattr(rag, "schedule_reindex", lambda bind: None)
    requests = []

    async def fake_embed(texts):
        requests.append(texts)
        vectors = []
        for value in texts:
            low = value.lower()
            if "claims.submission" in low or "подать" in low:
                vectors.append([1.0, 0.0, 0.0])
            elif "office:" in low:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors

    monkeypatch.setattr(rag, "embed_texts", fake_embed)
    return requests


def test_hybrid_public_index_and_exact_read(embeddings):
    async def run():
        engine = create_async_engine(settings.database_url)
        try:
            async with async_sessionmaker(engine)() as session:
                await load_kit(session, Path(settings.datasets_dir), reset=True)
                kb = Knowledge(session)
                before = await kb.search.rag_query("claim submission", ["kb"])
                assert before["search_mode"] == "lexical"
                assert before["degraded_reason"] == "index_incomplete"
                assert embeddings == []
                indexed = await kb.search.reindex(batch_size=16)
                assert indexed["status"] == "complete" and indexed["indexed"] > 0
                # Embeddings receive only allowlisted documents, never client/dev data.
                for batch in embeddings:
                    assert all(value.split(":", 1)[0] in rag.RAG_KINDS for value in batch)
                hybrid = await kb.search.rag_query("Как подать заявление?", ["kb"], 3)
                assert hybrid["search_mode"] == "hybrid"
                first = hybrid["hits"][0]
                assert first["key"] == "claims.submission"
                assert first["source_id"] == "kb:claims.submission"
                assert first["payload"]
                exact = await kb.read("kb", first["key"])
                assert exact.value == first["payload"]
                # A prefix/fuzzy query must not be substituted for a missing exact document.
                assert await kb.read("kb", "claim document submission") is None
                with pytest.raises(ValueError):
                    await kb.read("client", "C004")
                with pytest.raises(ValueError):
                    await kb.search.rag_query("C004", ["client"])
                with pytest.raises(ValueError):
                    await kb.search.rag_query("office", [])
                # Independent reindex transactions must not disturb caller's pending transaction.
                await session.rollback()
                assert (await kb.search.reindex())["indexed"] == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_vector_invalidation_and_model_change(embeddings, monkeypatch):
    async def run():
        engine = create_async_engine(settings.database_url)
        try:
            async with async_sessionmaker(engine)() as session:
                await load_kit(session, Path(settings.datasets_dir), reset=True)
                kb = Knowledge(session)
                await kb.search.reindex()
                await kb.store.put("kb", "claims.submission", {"text": "Changed submission terms"})
                count = await session.scalar(text(
                    "SELECT count(*) FROM knowledge_vectors "
                    "WHERE kind='kb' AND key='claims.submission'"
                ))
                assert count == 0
                await session.rollback()
                result = await kb.search.rag_query("submission", ["kb"])
                assert result["search_mode"] == "lexical"
                assert result["degraded_reason"] == "index_incomplete"
                assert (await kb.search.reindex())["indexed"] == 1
                monkeypatch.setattr(settings, "embedding_model", "new-embedding-model")
                assert (await kb.search.rag_query("submission", ["kb"]))[
                    "degraded_reason"
                ] == "index_incomplete"
                assert (await kb.search.reindex())["indexed"] > 1
                await kb.store.delete("kb", "claims.submission")
                count = await session.scalar(text(
                    "SELECT count(*) FROM knowledge_vectors "
                    "WHERE kind='kb' AND key='claims.submission'"
                ))
                assert count == 0
                await session.rollback()
                await kb.reload(reset=True)
                assert (await kb.search.rag_query("submission", ["kb"]))[
                    "degraded_reason"
                ] == "index_incomplete"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_no_transactions_during_embedding_and_changed_document(embeddings, monkeypatch):
    async def run():
        # A size-one connection pool would deadlock if retrieval/indexing held its connection
        # across a provider wait. The fake provider also changes the indexed document mid-call.
        engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0,
                                     pool_timeout=1)
        sessions = async_sessionmaker(engine)
        original_embed = rag.embed_texts
        try:
            async with sessions() as session:
                await load_kit(session, Path(settings.datasets_dir), reset=True)
            kb = Knowledge(sessions())

            async def inspect_connection(texts):
                async with sessions() as check:
                    assert await check.scalar(text("SELECT 1")) == 1
                return await original_embed(texts)

            monkeypatch.setattr(rag, "embed_texts", inspect_connection)
            assert (await kb.search.reindex())["status"] == "complete"

            async def change_document(texts):
                async with sessions() as update:
                    await Knowledge(update).store.put(
                        "kb", "claims.submission", {"text": "New public submission rule"}
                    )
                return await original_embed(texts)

            monkeypatch.setattr(rag, "embed_texts", change_document)
            result = await kb.search.rag_query("submission", ["kb"])
            assert result["search_mode"] == "lexical"
            assert result["degraded_reason"] == "index_changed"
            assert any(hit["payload"] == {"text": "New public submission rule"}
                       for hit in result["hits"])
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_provider_failure_and_missing_key_are_explicit(embeddings, monkeypatch):
    async def run():
        engine = create_async_engine(settings.database_url)
        try:
            async with async_sessionmaker(engine)() as session:
                await load_kit(session, Path(settings.datasets_dir), reset=True)
                kb = Knowledge(session)
                await kb.search.reindex()

                async def failure(texts):
                    raise ValueError("secret-provider-token")

                monkeypatch.setattr(rag, "embed_texts", failure)
                result = await kb.search.rag_query("submission", ["kb"])
                assert result["degraded_reason"] == "embedding_request_failed"
                assert "secret-provider-token" not in str(result)
                assert result["hits"]

                async def slow_provider(texts):
                    await asyncio.sleep(10)

                monkeypatch.setattr(rag, "embed_texts", slow_provider)
                monkeypatch.setattr(settings, "llm_timeout_seconds", 0.3)
                # The runtime's tool-wide timeout must not swallow the lexical fallback.
                async with asyncio.timeout(settings.llm_timeout_seconds):
                    result = await kb.search.rag_query("submission", ["kb"])
                assert result["search_mode"] == "lexical" and result["hits"]
                assert result["degraded_reason"] == "embedding_request_failed"
                monkeypatch.setattr(settings, "openai_api_key", "")
                result = await kb.search.rag_query("submission", ["kb"])
                assert result["degraded_reason"] == "embedding_key_missing"
                assert await kb.read("kb", "claims.submission") is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
