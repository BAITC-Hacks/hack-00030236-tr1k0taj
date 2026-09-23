"""No network / no DB: dev-set parsing and predictions format vs the kit's evaluate.py."""

import json
import subprocess
import sys

from evals.router import DEV_PATH, EVALUATE_PATH, load_utterances, parse_metrics

LANGS = {"ru", "kk", "mixed"}
TYPES = {"single", "multi_intent", "out_of_scope", "unclear"}


def test_load_utterances_matches_dev_set():
    utterances = load_utterances()
    assert len(utterances) == 104
    ids = [u["id"] for u in utterances]
    assert len(set(ids)) == len(ids)
    for u in utterances:
        assert u["lang"] in LANGS
        assert u["type"] in TYPES
        assert u["expected"]


def test_predictions_format_matches_evaluate_perfect_score(tmp_path):
    utterances = load_utterances()
    # Perfect predictions built straight from `expected`, in evaluate.py's own format.
    predictions = {u["id"]: list(u["expected"]) for u in utterances}
    predictions_path = tmp_path / "predictions.json"
    predictions_path.write_text(json.dumps(predictions, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(EVALUATE_PATH), str(predictions_path), str(DEV_PATH)],
        capture_output=True, text=True, check=True,
    )
    metrics = parse_metrics(proc.stdout)
    assert metrics["groups"]["all"] == {"n": 104, "primary_acc": 1.0, "full_match": 1.0}
    assert metrics["intent_recall"] == 1.0
