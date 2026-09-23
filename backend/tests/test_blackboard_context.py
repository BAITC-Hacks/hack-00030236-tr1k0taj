import asyncio
from copy import deepcopy

import pytest

from app.context import (
    Contexts,
    Fact,
    MemoryStore,
    SessionContext,
    Turn,
    conversation_history,
    ensure_blackboard,
    fingerprint_matches,
    handoff_summary,
    input_versions,
    kernel_history,
    put_record,
    router_view,
    select_records,
    update_task,
)
from app.kernel import Repository


def test_replacement_invalidates_only_dependent_records_and_guards_writes():
    state = {}
    city, _ = put_record(state, key="city", value="Almaty")
    unrelated, _ = put_record(state, key="language", value="ru")
    office, _ = put_record(state, key="office", value="Office A", source="tool",
                           depends_on=[city["record_id"]])
    answer, _ = put_record(state, key="answer", value="Office A", source="agent",
                           depends_on=[office["record_id"]])
    before = deepcopy(state)
    with pytest.raises(ValueError, match="record_version_conflict"):
        put_record(state, key="city", value="Astana", expected_record_id="wrong")
    with pytest.raises(ValueError, match="dependency_not_found"):
        put_record(state, key="city", value="Astana", depends_on=["missing"])
    with pytest.raises(ValueError, match="dependency_not_current"):
        put_record(state, key="city", value="Astana", depends_on=[answer["record_id"]])
    assert state == before

    newer, events = put_record(state, key="city", value="Astana",
                               expected_record_id=city["record_id"])
    board = ensure_blackboard(state)
    assert newer["supersedes"] == city["record_id"] and newer["version"] == 2
    assert board["records"][city["record_id"]]["status"] == "superseded"
    assert board["records"][office["record_id"]]["status"] == "stale"
    assert board["records"][answer["record_id"]]["status"] == "stale"
    assert {r["record_id"] for r in select_records(state)} == {
        newer["record_id"], unrelated["record_id"],
    }
    assert events[0]["visibility"] == "internal"


def test_task_scopes_missing_fingerprints_expiry_and_pause(monkeypatch):
    from app.context import blackboard

    monkeypatch.setattr(blackboard.time, "time", lambda: 100.0)
    state = {}
    update_task(state, "claim", title="Claim", focus=True)
    update_task(state, "policy", title="Policy")
    shared, _ = put_record(state, key="language", value="ru")
    local, _ = put_record(state, key="language", value="kk", task_id="claim")
    message, _ = put_record(state, key="$message", value="claim details", task_id="claim")
    version = input_versions(state, "claim", ["language", "missing", "$message"])
    assert version == {"language": local["record_id"], "missing": None,
                       "$message": message["record_id"]}
    assert fingerprint_matches(state, "claim", version)
    assert input_versions(state, "policy", ["language", "$message"]) == {
        "language": shared["record_id"], "$message": None,
    }
    put_record(state, key="missing", value=True, task_id="policy")
    assert fingerprint_matches(state, "claim", version)
    put_record(state, key="missing", value=None, task_id="claim")
    assert not fingerprint_matches(state, "claim", version)

    update_task(state, "claim", status="paused")
    paused = input_versions(state, "claim", ["language"])
    assert paused["language"] == local["record_id"]
    assert not fingerprint_matches(state, "claim", paused)
    update_task(state, "claim", status="active")
    assert fingerprint_matches(state, "claim", paused)
    expires, _ = put_record(state, key="temporary", value="quote", task_id="claim", expires_at=110)
    put_record(state, key="derived", value="quote terms", task_id="claim", source="agent",
               depends_on=[expires["record_id"]])
    expired_version = input_versions(state, "claim", ["derived"])
    monkeypatch.setattr(blackboard.time, "time", lambda: 120.0)
    assert not fingerprint_matches(state, "claim", expired_version)
    assert not select_records(state, "claim", ["temporary", "derived"])
    assert expires["record_id"] in state["blackboard"]["records"]
    update_task(state, "claim", status="cancelled")
    assert all(record["task_id"] is None for record in select_records(state, "claim"))


def test_invalid_data_and_cross_task_dependency_are_rejected():
    state = {}
    update_task(state, "claim")
    record, _ = put_record(state, key="city", value="Almaty", task_id="claim")
    for arguments, code in [
        ({"key": "x", "value": "x", "depends_on": [record["record_id"]]}, "task_mismatch"),
        ({"key": "$message", "value": "x"}, "task_scope"),
        ({"key": "x", "value": float("nan")}, "invalid_record_value"),
        ({"key": "x", "value": "x" * 16001}, "record_value_limit"),
        ({"key": "x", "value": "x", "expires_at": float("inf")}, "invalid_expiry"),
    ]:
        with pytest.raises(ValueError, match=code):
            put_record(state, **arguments)
    with pytest.raises(ValueError, match="invalid_blackboard_schema"):
        ensure_blackboard({"blackboard": {"schema_version": 999}})

    # ASCII API task IDs include colon; scope/key encoding must remain collision-free.
    update_task(state, "a")
    update_task(state, "a:b")
    put_record(state, key="b:c", value="first", task_id="a")
    put_record(state, key="c", value="second", task_id="a:b")
    assert select_records(state, "a", ["b:c"])[0]["value"] == "first"
    assert select_records(state, "a:b", ["c"])[0]["value"] == "second"


def test_canonical_history_unifies_domain_runtime_and_delivery():
    context = SessionContext(session_id="s", history=[
        Turn(turn_id=1, role="client", text="Claim question", language="ru"),
        Turn(turn_id=1, role="bot", text="A whole answer"),
        Turn(turn_id=2, role="client", text="Bye"),
        Turn(turn_id=2, role="bot", text="Goodbye"),
    ], kernel={
        "messages": [{"turn_id": 1, "text": "Claim question", "response_id": "r",
                      "task_id": "claim"}],
        "responses": {"r": {
            "status": "interrupted", "played_ms": 150, "text_segment_ids": [],
            "timeline": {"a": {"start_ms": 0, "end_ms": 100},
                         "b": {"start_ms": 100, "end_ms": 200}},
            "segments": [{"segment_id": "a", "text": "Heard. ", "status": "completed"},
                         {"segment_id": "b", "text": "Whole partial sentence.",
                          "status": "interrupted"}],
        }},
    })
    history = conversation_history(context)
    assert len(history) == 4
    response = history[1]
    assert response["text"] == "Heard. Whole partial sentence."
    assert response["status"] == "interrupted" and response["task_id"] == "claim"
    assert [s["delivery"]["status"] for s in response["segments"]] == ["heard", "partially_heard"]
    assert response["segments"][1]["text"] == "Whole partial sentence."
    assert history[-1]["text"] == "Goodbye" and history[-1]["delivery"]["status"] == "unknown"
    assert router_view(context)["recent_turns"] == history[2:]
    assert router_view(context, task_id="claim")["recent_turns"] == history[:2]
    assert handoff_summary(context)["conversation_history"] == history
    assert kernel_history(context.kernel)[1] == response


def test_context_projection_and_call_ledger_stay_private_and_reload():
    async def run():
        store = MemoryStore()
        contexts = Contexts(store)
        repository = Repository(contexts=contexts)
        state = await repository.create([], {}, "mock", session_id="s")
        await contexts.begin_turn("s", "Claim question")
        async with contexts.mutate("s") as mutation:
            mutation.ctx.call_journal = {"seq": 5, "requests": {"secret-request": {"status": "done"}}}
        def publish(kernel, seq):
            return put_record(kernel, key="city", value="Almaty", task_id="default")

        await repository.change("s", publish)
        fresh = await Repository(contexts=Contexts(store)).get("s")
        assert fresh["conversation_history"][0]["text"] == "Claim question"
        assert fresh["last_seq"] > state["last_seq"]
        assert "conversation_history" not in await contexts.runtime_get("s")
        assert "conversation_history" not in store.states["s"].kernel
        public = (await contexts.snapshot("s")).model_dump()
        assert "kernel" not in public and "call_journal" not in public
        assert all(row.payload.get("type") != "blackboard.updated" for row in await contexts.board("s"))
        await contexts.start_call("s")
        assert (await contexts.snapshot("s")).call_journal["seq"] == 5

    asyncio.run(run())


def test_template_history_keeps_task_scope_and_rejects_old_generation_map():
    context = SessionContext(session_id="s", generation=2, history=[
        Turn(turn_id=1, role="client", text="Claim status"),
        Turn(turn_id=1, role="bot", text="Claim template"),
        Turn(turn_id=2, role="client", text="General question"),
    ], kernel={"generation": 2, "turn_tasks": {"1": "claim"}})
    history = conversation_history(context)
    assert [item["task_id"] for item in history] == ["claim", "claim", "default"]
    assert [item["text"] for item in history if (item["task_id"] or "default") == "default"] == [
        "General question",
    ]
    context.kernel["generation"] = 1
    assert all(item["task_id"] == "default" for item in conversation_history(context))


def test_context_runtime_slot_stays_opaque_to_blackboard_projection():
    class Owner:
        active = ("alive", True)

        def initial(self, _context, _spec):
            return {"alive": True, "custom": "initial"}, []

        def restarts_generation(self, _slot):
            return False

    async def run():
        contexts = Contexts(MemoryStore())
        contexts.attach_runtime(Owner())
        created = await contexts.runtime_create("opaque", {})
        assert created == {"alive": True, "custom": "initial", "last_seq": 0}

        def change(slot, _seq):
            slot["custom"] = "updated"
            return None, []

        await contexts.runtime_change("opaque", change)
        loaded = await contexts.runtime_get("opaque")
        assert loaded == {"alive": True, "custom": "updated", "last_seq": 0}
        assert "blackboard" not in (await contexts.snapshot("opaque")).kernel

    asyncio.run(run())


def test_domain_tasks_restore_confirmation_and_keep_router_history_isolated():
    async def run():
        store = MemoryStore()
        contexts = Contexts(store)
        await contexts.start_call("s")
        async with contexts.mutate("s") as mutation:
            mutation.set_client("C004")
            mutation.focus_task("claim")
            mutation.switch_topic("SC17")
            mutation.set_slots("SC17", {"claim_number": "CL-500311"})
            mutation.add_facts([Fact(key="claim.status", value="requested", source="get_claim")])
            mutation.request_confirmation("send_documents", {"claim_number": "CL-500311"})
            mutation.record_routing({}, 0.2)
        await contexts.begin_turn("s", "Claim question", "ru")
        claim = await contexts.snapshot("s")
        async with contexts.mutate("s") as mutation:
            mutation.focus_task("policy")
        await contexts.begin_turn("s", "Policy question", "kk")
        policy = await contexts.snapshot("s")
        view = router_view(policy)
        assert policy.client_id == "C004" and policy.language == "kk"
        assert view["known_slots"] == {} and view["facts"] == []
        assert view["pending_confirmation"] is None and policy.low_confidence_streak == 0
        assert [item["text"] for item in view["recent_turns"]] == ["Policy question"]
        assert "task_states" not in policy.model_dump()
        contexts = Contexts(store)
        async with contexts.mutate("s") as mutation:
            mutation.focus_task("claim")
        restored = await contexts.snapshot("s")
        assert restored.slots_by_topic == claim.slots_by_topic
        assert restored.pending_confirmation == claim.pending_confirmation
        assert restored.facts == claim.facts and restored.low_confidence_streak == 1
        assert [item["text"] for item in router_view(restored)["recent_turns"]] == ["Claim question"]
        async with contexts.mutate("s") as mutation:
            mutation.focus_task("claim")
        assert (await contexts.snapshot("s")).context_version == restored.context_version
        reset = await contexts.start_call("s")
        assert reset.task_states == {} and reset.domain_task_id == "default"

    asyncio.run(run())
