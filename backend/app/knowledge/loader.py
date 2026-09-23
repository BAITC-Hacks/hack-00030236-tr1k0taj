"""Load the starter kit (datasets/*.json) into kit_records.

Kit files are never modified. dev_utterances.json and dialogs_sample.json are NOT loaded:
expected labels belong to the evaluator only (ADR 0007).
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.expansions import load_expansions, payload_hash, variant_texts
from app.knowledge.models import KitRecord

# A KB node whose JSON is shorter than this becomes one searchable chunk; bigger nodes are split.
KB_CHUNK_CHARS = 600
# KB lists that get their own record kind instead of being KB chunks.
KB_LIST_KINDS = {"offices": "office", "clinics": "clinic", "inspection_points": "inspection_point"}


def _texts(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for v in node.values():
            yield from _texts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _texts(v)
    elif node is not None and not isinstance(node, bool):
        yield str(node)


def search_text(payload: Any, *extra: str) -> str:
    return " ".join([*extra, *_texts(payload)])


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _kb_chunks(node: Any, path: str) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict) and len(json.dumps(node, ensure_ascii=False)) > KB_CHUNK_CHARS:
        for k, v in node.items():
            yield from _kb_chunks(v, f"{path}.{k}")
    else:
        yield path, node


def _record(kind: str, key: str, payload: Any, *extra_text: str) -> dict:
    return {
        "kind": kind,
        "key": key,
        "payload": payload,
        "search_text": search_text(payload, *extra_text),
        "origin": "kit",
    }


def doc_text(kind: str, key: str, payload: Any) -> str:
    """Original searchable text of a record (what build_records puts in search_text)."""
    extra = [key.replace(".", " ").replace("_", " ")] if kind == "kb" else []
    return search_text(payload, *extra)


def fresh_expansion(kind: str, key: str, payload: Any, expansions: dict[str, dict]) -> dict | None:
    """Expansion generated for exactly this payload; stale ones (record changed) are ignored."""
    exp = expansions.get(f"{kind}/{key}")
    return exp if exp and exp.get("hash") == payload_hash(payload) else None


def document_variants(
    kind: str, key: str, payload: Any, expansions: dict[str, dict]
) -> list[tuple[str, str]]:
    """(lang, text) that represent a record for semantic search: the original text first,
    then ru/kk/mixed client questions and summaries from expansions.json (ADR 0010)."""
    variants = [("doc", doc_text(kind, key, payload))]
    if exp := fresh_expansion(kind, key, payload, expansions):
        variants += variant_texts(exp)
    return variants


def apply_expansions(records: list[dict], expansions: dict[str, dict]) -> list[dict]:
    """Append expansion texts to search_text: Kazakh/Russian words enter the lexical index, so
    ru/kk questions find English KB entries even without an embedding key."""
    for r in records:
        variants = document_variants(r["kind"], r["key"], r["payload"], expansions)
        if len(variants) > 1:
            r["search_text"] = " ".join(text for _, text in variants)
    return records


def _unique_keys(items: list[dict], base) -> Iterator[tuple[str, dict]]:
    seen: dict[str, int] = {}
    for item in items:
        key = base(item)
        seen[key] = seen.get(key, 0) + 1
        yield (key if seen[key] == 1 else f"{key}-{seen[key]}"), item


def build_records(datasets_dir: Path) -> list[dict]:
    """Pure function: kit files -> rows for kit_records."""

    def read(name: str) -> dict:
        return json.loads((datasets_dir / name).read_text(encoding="utf-8"))

    rows: list[dict] = []

    scenarios = read("scenarios.json")
    rows += [_record("scenario", s["scenario_id"], s) for s in scenarios["scenarios"]]
    rows += [_record("system_intent", s["id"], s) for s in scenarios["system_intents"]]

    actions = read("actions.json")
    rows += [_record("action", a["name"], a) for a in actions["actions"]]
    rows += [_record("queue", q, {"name": q}) for q in actions["queues"]]
    rows += [
        _record("error_code", code, {"code": code, "description": desc})
        for code, desc in actions["error_codes"].items()
    ]
    rows.append(_record("meta", "actions.error_handling", actions["error_handling"]))
    rows.append(_record("meta", "actions.error_format", actions["error_format"]))

    rows += [_record("slot", s["name"], s) for s in read("slots.json")["slots"]]

    backend = read("mock_backend.json")
    rows += [_record("client", c["client_id"], c) for c in backend["clients"]]
    rows += [_record("policy", p["policy_number"], p) for p in backend["policies"]]
    rows += [_record("claim", c["claim_number"], c) for c in backend["claims"]]
    rows += [_record("payment", p["payment_id"], p) for p in backend["payments"]]
    rows.append(_record("meta", "mock_backend.defaults", backend["defaults"]))

    kb = read("knowledge_base.json")
    for section, kind in KB_LIST_KINDS.items():
        base = (lambda x: _slug(x["name"])) if kind == "clinic" else (lambda x: _slug(x["city"]))
        rows += [_record(kind, k, item) for k, item in _unique_keys(kb[section], base)]
    for section, node in kb.items():
        if section == "meta" or section in KB_LIST_KINDS:
            continue
        for path, value in _kb_chunks(node, section):
            rows.append(_record("kb", path, value, path.replace(".", " ").replace("_", " ")))

    return rows


async def load_kit(session: AsyncSession, datasets_dir: Path, *, reset: bool = False) -> int:
    """Upsert kit rows. reset=True drops everything first (incl. CRUD edits) — clean kit state.

    Without reset, rows edited through CRUD (origin=user) are kept. search_text includes the
    ru/kk expansions; a changed search_text invalidates the record's vectors (DB trigger).
    """
    rows = apply_expansions(build_records(datasets_dir), load_expansions())
    if reset:
        await session.execute(delete(KitRecord))
    stmt = insert(KitRecord).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[KitRecord.kind, KitRecord.key],
        set_={"payload": stmt.excluded.payload, "search_text": stmt.excluded.search_text},
        where=KitRecord.origin == "kit",
    )
    await session.execute(stmt)
    await session.commit()
    from app.knowledge.rag import clear_knowledge_cache, schedule_reindex

    # Kit rows changed: query embeddings and rag_query results cached from before are stale.
    clear_knowledge_cache()
    schedule_reindex(session.bind)
    return len(rows)


async def ensure_loaded(session: AsyncSession, datasets_dir: Path) -> None:
    """Load the kit on startup if the table is empty."""
    count = await session.scalar(select(func.count()).select_from(KitRecord))
    if not count:
        await load_kit(session, datasets_dir)
