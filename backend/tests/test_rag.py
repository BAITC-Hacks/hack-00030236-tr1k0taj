"""Real pgvector/FTS integration; embedding HTTP is always replaced with deterministic vectors."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.knowledge import Knowledge, load_kit, loader, rag
from app.knowledge.expansions import load_expansions, variant_texts


@pytest.fixture
def embeddings(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "unit-test-key")
    monkeypatch.setattr(settings, "embedding_model", "test-embedding-model")
    monkeypatch.setattr(settings, "embedding_dimensions", 3)
    # Index explicitly so background tasks do not compete with transaction assertions.
    monkeypatch.setattr(rag, "schedule_reindex", lambda bind: None)
    # Mechanics tests use toy vectors: keep one vector per record (no ru/kk expansions).
    # Expansions are covered by test_expansion_variants and measured by `just eval-search`.
    monkeypatch.setattr(rag, "load_expansions", dict)
    monkeypatch.setattr(loader, "load_expansions", dict)
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


def _private_values() -> list[str]:
    """Client/policy/claim/payment field values and dev utterances from the kit."""
    root = Path(settings.datasets_dir)
    backend = json.loads((root / "mock_backend.json").read_text(encoding="utf-8"))
    values = [
        str(record[field])
        for section, fields in {
            "clients": ("full_name", "phone", "iin", "email", "address"),
            "policies": ("policy_number",),
            "claims": ("claim_number",),
            "payments": ("payment_id",),
        }.items()
        for record in backend[section]
        for field in fields
    ]
    dev = json.loads((root / "dev_utterances.json").read_text(encoding="utf-8"))
    return values + [u["text"] for u in dev["utterances"]]


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
                # Embeddings receive only allowlisted documents, never client/dev data. Document
                # vectors carry a "kind: key" header; ru/kk expansion questions do not, so check
                # the content: no client field value and no dev utterance is ever embedded.
                sent = "\n".join(value for b in embeddings for value in b)
                assert not [v for v in _private_values() if v in sent]
                hybrid = await kb.search.rag_query("Как подать заявление?", ["kb"], 3)
                assert hybrid["search_mode"] == "hybrid"
                first = hybrid["hits"][0]
                assert first["key"] == "claims.submission"
                assert first["source_id"] == "kb:claims.submission"
                assert first["payload"]
                # search_query is embedded together with the client's words, one request
                await kb.search.rag_query(
                    "Құжатты қалай беремін?", ["kb"], search_query="claims.submission"
                )
                assert embeddings[-1] == ["Құжатты қалай беремін?", "claims.submission"]
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
                count = await session.scalar(
                    text(
                        "SELECT count(*) FROM knowledge_vectors "
                        "WHERE kind='kb' AND key='claims.submission'"
                    )
                )
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
                count = await session.scalar(
                    text(
                        "SELECT count(*) FROM knowledge_vectors "
                        "WHERE kind='kb' AND key='claims.submission'"
                    )
                )
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
        engine = create_async_engine(
            settings.database_url, pool_size=1, max_overflow=0, pool_timeout=1
        )
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
            assert any(
                hit["payload"] == {"text": "New public submission rule"} for hit in result["hits"]
            )
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


def test_expansion_variants(embeddings, monkeypatch):
    """ru/kk expansions: several vectors per record, coverage per record, Kazakh words in the
    lexical index, and still no private data sent to the embedding provider."""
    monkeypatch.setattr(rag, "load_expansions", load_expansions)
    monkeypatch.setattr(loader, "load_expansions", load_expansions)

    async def run():
        engine = create_async_engine(settings.database_url)
        try:
            async with async_sessionmaker(engine)() as session:
                await load_kit(session, Path(settings.datasets_dir), reset=True)
                kb = Knowledge(session)
                # Kazakh finds the English KB entry lexically, without any embedding
                hits = await kb.search.query(
                    "Полисімді жапсам, ақшамды қайтарасыздар ма?", ["kb"], 3
                )
                assert "cancellation" in [h.key for h in hits]

                assert (await kb.search.reindex())["status"] == "complete"
                vectors = await session.scalar(
                    text(
                        "SELECT count(*) FROM knowledge_vectors WHERE kind = 'kb' AND key = 'cancellation'"
                    )
                )
                assert vectors == 1 + len(variant_texts(load_expansions()["kb/cancellation"]))
                result = await kb.search.rag_query("ақша қайтару", ["kb"], 3)
                assert result["search_mode"] == "hybrid"  # coverage counts records, not vectors

                sent = "\n".join(value for batch in embeddings for value in batch)
                assert not [v for v in _private_values() if v in sent]
        finally:
            await engine.dispose()

    asyncio.run(run())
