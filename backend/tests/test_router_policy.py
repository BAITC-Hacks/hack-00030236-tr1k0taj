from app.router import RouterOutput, decide

PRIORITIES = {"SC11": "urgent", "SC17": "normal", "SC18": "normal", "SC33": "normal"}


def out(*picks, alternatives=()):
    return RouterOutput.model_validate(
        {
            "scenarios": [{"scenario_id": i, "confidence": c} for i, c in picks],
            "alternatives": [{"scenario_id": i, "confidence": c} for i, c in alternatives],
            "language": "ru",
        }
    )


def test_unknown_scenario_id_is_reported():
    assert out(("SC17", 0.9), alternatives=[("SC99", 0.2)]).unknown_ids({"SC17"}) == ["SC99"]


def test_thresholds_route_clarify_handoff():
    assert decide(out(("SC17", 0.8)), PRIORITIES, 0).kind == "route"
    mid = decide(out(("SC17", 0.6), alternatives=[("SC18", 0.5)]), PRIORITIES, 0)
    assert mid.kind == "clarify" and mid.clarify_options == ["SC17", "SC18"]
    assert decide(out(("SC17", 0.3)), PRIORITIES, 0).kind == "clarify"
    assert decide(out(("SC17", 0.3)), PRIORITIES, 1).kind == "handoff"


def test_urgent_goes_first():
    d = decide(out(("SC33", 0.9), ("SC11", 0.85)), PRIORITIES, 0)
    assert [s.scenario_id for s in d.scenarios] == ["SC11", "SC33"]
