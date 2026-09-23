"""Index-time expansion: typical client questions in ru / kk / mixed for every KB and reference
record. KB is in English, clients speak Russian and Kazakh; the questions bridge the gap for
semantic AND lexical search (Kazakh words end up in the index).

Generated once by an LLM (`just expand-kb`), committed to expansions.json, loaded at startup
without any LLM call. Facts still come from the original kit data — expansions only help
to FIND a record. The generator never sees dev_utterances.json or the retrieval eval set.
"""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

EXPANSIONS_PATH = Path(__file__).with_name("expansions.json")
EXPAND_KINDS = ["kb", "office", "clinic", "inspection_point"]
LANGS = ["ru", "kk", "mixed"]

PROMPT = """You index the knowledge base of Saqta Insurance (a general insurer in Kazakhstan)
for a voice bot. Clients speak Russian, Kazakh, or mix both languages in one sentence.

For the entry below return JSON:
{{"ru": [6 questions], "kk": [6 questions], "mixed": [3 questions],
 "summary_ru": "one sentence", "summary_kk": "one sentence"}}

Rules:
- Questions a client would actually say on the phone, answerable by this entry.
- Colloquial spoken style, vary wording and length; no numbering.
- "kk": natural modern Kazakh (Cyrillic), as spoken in Almaty/Astana; not a word-by-word
  translation of Russian.
- "mixed": Kazakh sentence with Russian words or vice versa, as people really code-switch.
- Keep product names clients use: ОГПО, КАСКО, ДМС, полис.

Entry kind: {kind}
Entry key: {key}
Entry data: {data}"""


def payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_expansions(path: Path = EXPANSIONS_PATH) -> dict[str, dict]:
    """{'kb/claims.submission': {'hash': ..., 'ru': [...], 'kk': [...], ...}}; {} if absent."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["records"]


def _norm(value: str) -> str:
    return " ".join(value.lower().split())


def drop_dev_overlap(records: dict[str, dict], datasets_dir: Path) -> int:
    """Remove generated questions that coincide verbatim with dev_utterances.json. The generator
    never sees the dev set, but common phrases can collide; dev strings must not enter the
    index (AGENTS.md invariant). Only exact text is compared — labels are never read."""
    dev = json.loads((datasets_dir / "dev_utterances.json").read_text(encoding="utf-8"))
    banned = {_norm(u["text"]) for u in dev["utterances"]}
    dropped = 0
    for exp in records.values():
        for lang in LANGS:
            kept = [q for q in exp.get(lang, []) if _norm(q) not in banned]
            dropped += len(exp.get(lang, [])) - len(kept)
            exp[lang] = kept
    return dropped


def variant_texts(expansion: dict) -> list[tuple[str, str]]:
    """(lang, text) pairs for kit_variants; summaries count as their language."""
    out = [(lang, q) for lang in LANGS for q in expansion.get(lang, []) if q]
    out += [
        (lang, expansion[f"summary_{lang}"])
        for lang in ("ru", "kk")
        if expansion.get(f"summary_{lang}")
    ]
    return out


async def generate(
    records: list[dict],
    api_key: str,
    model: str,
    datasets_dir: Path,
    *,
    force: bool = False,
    path: Path = EXPANSIONS_PATH,
) -> tuple[int, int]:
    """Generate expansions for EXPAND_KINDS records whose payload changed. Returns (new, kept)."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, timeout=120, max_retries=2)
    existing = {} if force else load_expansions(path)
    targets = [r for r in records if r["kind"] in EXPAND_KINDS]
    todo = [
        r
        for r in targets
        if existing.get(f"{r['kind']}/{r['key']}", {}).get("hash") != payload_hash(r["payload"])
    ]
    sem = asyncio.Semaphore(8)

    async def one(r: dict) -> tuple[str, dict]:
        async with sem:
            resp = await client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "user",
                        "content": PROMPT.format(
                            kind=r["kind"],
                            key=r["key"],
                            data=json.dumps(r["payload"], ensure_ascii=False),
                        ),
                    }
                ],
            )
            data = json.loads(resp.choices[0].message.content)
            clean = {
                lang: [str(q).strip() for q in data.get(lang, []) if str(q).strip()]
                for lang in LANGS
            }
            for lang in ("ru", "kk"):
                clean[f"summary_{lang}"] = str(data.get(f"summary_{lang}", "")).strip()
            return f"{r['kind']}/{r['key']}", {"hash": payload_hash(r["payload"]), **clean}

    fresh = dict(await asyncio.gather(*(one(r) for r in todo)))
    keep = {f"{r['kind']}/{r['key']}" for r in targets}
    merged = {k: v for k, v in {**existing, **fresh}.items() if k in keep}
    drop_dev_overlap(merged, datasets_dir)
    path.write_text(
        json.dumps(
            {
                "meta": {"model": model, "kinds": EXPAND_KINDS},
                "records": dict(sorted(merged.items())),
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(fresh), len(merged) - len(fresh)
