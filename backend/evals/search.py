"""KB retrieval eval over rag_query (the path the kernel uses): recall@1 / recall@3 by language.

    python -m evals.search                      # needs the stack; hybrid modes need a key
    python -m evals.search --fill-search-query  # (re)generate English rewrites with an LLM

Modes:
- lexical   — embeddings off (what a reviewer without a key gets)
- hybrid    — lexical + vectors on the raw client text (rank fusion)
- hybrid+sq — plus the English search_query (simulates the router output)
"""

import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.knowledge import Knowledge, reindex_knowledge
from app.knowledge.expansions import load_expansions

QUERIES_PATH = Path(__file__).with_name("retrieval_queries.json")
LANGS = ["ru", "kk", "mixed"]
REWRITE_MODEL = "gpt-4.1-mini"  # fast model, like the router would use
REWRITE_PROMPT = (
    "A client of an insurance company said (in Russian, Kazakh or mixed): {text}\n"
    "Rewrite it as a short English search query for the company knowledge base. "
    "Return only the query."
)


def load() -> dict:
    return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))


async def fill_search_query() -> None:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    data = load()

    async def one(q: dict) -> None:
        r = await client.chat.completions.create(
            model=REWRITE_MODEL,
            temperature=0,
            messages=[{"role": "user", "content": REWRITE_PROMPT.format(text=q["text"])}],
        )
        q["search_query"] = r.choices[0].message.content.strip().strip('"')

    await asyncio.gather(*(one(q) for q in data["queries"]))
    QUERIES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"search_query filled for {len(data['queries'])} queries -> {QUERIES_PATH}")


def leakage(queries: list[dict]) -> list[str]:
    """Eval questions must not appear verbatim in the generated expansions."""
    texts = {
        t.strip().lower()
        for e in load_expansions().values()
        for lang in LANGS
        for t in e.get(lang, [])
    }
    return [q["id"] for q in queries if q["text"].strip().lower() in texts]


async def run() -> None:
    queries = load()["queries"]
    if leaked := leakage(queries):
        print(f"WARNING: eval questions found verbatim in expansions: {leaked}")

    hybrid = bool(settings.openai_api_key) and settings.embeddings_enabled
    if hybrid:
        print("reindex:", await reindex_knowledge())
    else:
        print("no OPENAI_API_KEY / embeddings disabled: hybrid modes skipped")
    modes = ["lexical", *(["hybrid", "hybrid+sq"] if hybrid else [])]

    stats: dict = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
    search_modes: dict[str, Counter] = defaultdict(Counter)
    misses: dict[str, list[str]] = defaultdict(list)
    enabled = settings.embeddings_enabled
    async with SessionLocal() as session:
        kb = Knowledge(session)
        for mode in modes:
            settings.embeddings_enabled = enabled and mode != "lexical"
            for q in queries:
                sq = q.get("search_query") if mode == "hybrid+sq" else None
                result = await kb.search.rag_query(q["text"], ["kb"], 3, search_query=sq)
                search_modes[mode][result["search_mode"]] += 1
                ranked = [h["key"] for h in result["hits"]]
                for bucket in (q["lang"], "all"):
                    s = stats[mode][bucket]
                    s[0] += ranked[:1] == [q["expected"]]
                    s[1] += q["expected"] in ranked
                    s[2] += 1
                if ranked[:1] != [q["expected"]]:
                    misses[mode].append(f"{q['id']} {q['expected']} <- {ranked}")
    settings.embeddings_enabled = enabled

    print("\n| mode | " + " | ".join(f"{b} r@1 / r@3" for b in [*LANGS, "all"]) + " |")
    print("|---|" + "---|" * (len(LANGS) + 1))
    for mode in modes:
        cells = []
        for b in [*LANGS, "all"]:
            r1, r3, n = stats[mode][b]
            cells.append(f"{r1 / n:.2f} / {r3 / n:.2f}" if n else "-")
        print(f"| {mode} | " + " | ".join(cells) + " |")
    print(f"\nn = { {b: stats['lexical'][b][2] for b in [*LANGS, 'all']} }")
    print("search_mode per run:", {m: dict(c) for m, c in search_modes.items()})
    for mode in modes:
        print(f"\nmisses@1 [{mode}]:")
        for m in misses[mode]:
            print("  ", m)


if __name__ == "__main__":
    if "--fill-search-query" in sys.argv:
        asyncio.run(fill_search_query())
    else:
        asyncio.run(run())
