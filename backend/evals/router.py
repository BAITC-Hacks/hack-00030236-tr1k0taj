"""Router accuracy eval on the 104 dev utterances (ADR 0007, spec 11): runs the real
LLM router (no mock), then scores it with the kit's own `evaluate.py`.

    python -m evals.router     # needs the stack (DB) + OPENAI_API_KEY

Each dev utterance is routed as a fresh, context-free call — `expected` is never passed
to the router, only read afterwards by `evaluate.py`. Predictions are written next to
this file (gitignored) in the `{"U001": ["SC01"], ...}` format `evaluate.py` expects,
which is then invoked as a subprocess so the printed table stays the reference one.
A `router_eval_result.json` with the same numbers (+ ru/kk/mixed and type breakdown) is
saved alongside for the README / CI to pick up.
"""

import asyncio
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.context import SessionContext, router_view
from app.knowledge import open_knowledge
from app.router import OpenAIRouter, RouterUnavailable

DATASETS_DIR = Path(settings.datasets_dir)
DEV_PATH = DATASETS_DIR / "dev_utterances.json"
EVALUATE_PATH = DATASETS_DIR / "evaluate.py"
PREDICTIONS_PATH = Path(__file__).with_name("predictions.json")
RESULT_PATH = Path(__file__).with_name("router_eval_result.json")
CONCURRENCY = 8

GROUP_RE = re.compile(r"^(\S+)\s+(\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)$", re.MULTILINE)
RECALL_RE = re.compile(r"intent_recall \(multi-intent\): (\d+\.\d+)")


def load_utterances() -> list[dict]:
    return json.loads(DEV_PATH.read_text(encoding="utf-8"))["utterances"]


def parse_metrics(stdout: str) -> dict:
    """Reparse evaluate.py's printed table into JSON; evaluate.py itself stays untouched."""
    groups = {
        key: {"n": int(n), "primary_acc": float(p), "full_match": float(f)}
        for key, n, p, f in GROUP_RE.findall(stdout)
    }
    m = RECALL_RE.search(stdout)
    return {"groups": groups, "intent_recall": float(m.group(1)) if m else None}


async def route_one(router: OpenAIRouter, sem: asyncio.Semaphore, u: dict, progress: list[int]) -> tuple[str, list[str]]:
    async with sem:
        scenarios: list[str] = []
        try:
            async with open_knowledge() as kb:
                snap = SessionContext(session_id=f"eval-{u['id']}")
                rr = await router.route(u["text"], router_view(snap), kb)
                scenarios = [s.scenario_id for s in rr.output.scenarios]
        except RouterUnavailable as e:
            print(f"  ! {u['id']}: {e.code}: {e.message}")
        progress[0] += 1
        print(f"[{progress[0]:>3}/{progress[1]}] {u['id']} ({u['lang']}) -> {scenarios}")
        return u["id"], scenarios


async def run(utterances: list[dict]) -> dict[str, list[str]]:
    router = OpenAIRouter()
    sem = asyncio.Semaphore(CONCURRENCY)
    progress = [0, len(utterances)]
    pairs = await asyncio.gather(*(route_one(router, sem, u, progress) for u in utterances))
    return dict(pairs)


def main() -> None:
    if not settings.openai_api_key:
        sys.exit(
            "OPENAI_API_KEY is not set: `just eval` runs the real router, not the mock. "
            "Set it in .env / the environment and retry (see README)."
        )
    utterances = load_utterances()
    predictions = asyncio.run(run(utterances))
    PREDICTIONS_PATH.write_text(
        json.dumps(predictions, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    proc = subprocess.run(
        [sys.executable, str(EVALUATE_PATH), str(PREDICTIONS_PATH), str(DEV_PATH)],
        capture_output=True, text=True, check=False,
    )
    print("\n" + proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        sys.exit(proc.returncode)

    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": settings.llm_model,
        "n": len(utterances),
        **parse_metrics(proc.stdout),
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"result -> {RESULT_PATH}")


if __name__ == "__main__":
    main()
