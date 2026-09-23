"""Hybrid lexical search over kit_records: Postgres FTS ('simple', no stemming — same for ru/kk/en)
plus pg_trgm word similarity (catches ru/kk morphology: 'өтінішім' ~ 'өтініш').

Scenario hits are HINTS for trace/debug only. The router always receives the full catalog;
search results must never narrow it (spec 5.5, README kit: no intent classifiers).

KB, offices and clinics are in English. Callers pass kit vocabulary (normalized slot values,
a KB topic from topics()); cross-lingual search on raw ru/kk text is phase 2 (embeddings).
"""

import re

from sqlalchemy import text

from app.knowledge.store import Store
from app.knowledge.types import Fact, Hit

MIN_SCORE = 0.15

_SEARCH_SQL = text(
    """
    SELECT kind, key, payload, left(search_text, 240) AS snippet,
           ts_rank(tsv, to_tsquery('simple', :tsq)) + word_similarity(:q, search_text) AS score
    FROM kit_records
    WHERE kind = ANY(:kinds)
      AND (tsv @@ to_tsquery('simple', :tsq) OR word_similarity(:q, search_text) > 0.3)
    ORDER BY score DESC, kind, key
    LIMIT :limit
    """
)

SEARCHABLE_KINDS = ["kb", "office", "clinic", "inspection_point", "scenario", "system_intent"]


def _tsquery(query: str) -> str:
    # OR over tokens: short voice utterances rarely share every word with a record.
    return " | ".join(re.findall(r"\w+", query.lower()))


class Search:
    def __init__(self, store: Store):
        self._store = store

    async def query(
        self, q: str, kinds: list[str] | None = None, limit: int = 5
    ) -> list[Hit]:
        tsq = _tsquery(q)
        if not tsq:
            return []
        rows = await self._store.session.execute(
            _SEARCH_SQL,
            {"q": q, "tsq": tsq, "kinds": kinds or SEARCHABLE_KINDS, "limit": limit},
        )
        return [
            Hit(kind=r.kind, key=r.key, score=round(r.score, 4), snippet=r.snippet, payload=r.payload)
            for r in rows
            if r.score >= MIN_SCORE
        ]

    async def topics(self) -> list[str]:
        """KB topic index (dotted paths) — give it to the LLM so it picks a topic for kb_lookup."""
        return await self._store.keys("kb")

    async def kb_lookup(self, topic: str) -> Fact | None:
        """Exact KB path, a section prefix ('claims.documents'), or free text as a fallback."""
        topic = topic.strip()
        value = await self._store.get("kb", topic)
        if value is not None:
            return Fact(key=f"kb.{topic}", value=value, source="kb_lookup", source_id=topic)

        children = {
            k.removeprefix(topic + "."): v for k, v in await self._store.items("kb", topic + ".")
        }
        if children:
            return Fact(key=f"kb.{topic}", value=children, source="kb_lookup", source_id=topic)

        hits = await self.query(topic, kinds=["kb"], limit=1)
        if hits:
            h = hits[0]
            return Fact(key=f"kb.{h.key}", value=h.payload, source="kb_lookup", source_id=h.key)
        return None

    async def offices(self, city: str) -> list[Fact]:
        return await self._by_city("office", city, "get_offices")

    async def inspection_points(self, city: str) -> list[Fact]:
        return await self._by_city("inspection_point", city, "kb_lookup")

    async def clinics(self, city: str, specialty: str | None = None) -> list[Fact]:
        facts = await self._by_city("clinic", city, "list_clinics")
        if specialty:
            s = specialty.lower()
            facts = [f for f in facts if s in (x.lower() for x in f.value.get("specialties", []))]
        return facts

    async def _by_city(self, kind: str, city: str, source: str) -> list[Fact]:
        c = city.strip().lower()
        return [
            Fact(key=f"{kind}.{key}", value=payload, source=source, source_id=key)
            for key, payload in await self._store.items(kind)
            if payload.get("city", "").lower() == c
        ]
