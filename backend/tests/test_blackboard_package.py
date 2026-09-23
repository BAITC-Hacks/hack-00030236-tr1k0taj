"""Task/agent context boundaries and bounded long-conversation projection."""

import json
from copy import deepcopy

import pytest

from app.context import ensure_blackboard, put_record, update_task
from app.kernel.context import MAX_PACKAGE_CHARS, package
from app.kernel.provider import _validated_arguments


def state():
    value = {
        "session_id": "test", "generation": 1, "input_revision": 2, "status": "open",
        "messages": [{"turn_id": 1, "text": "Нужен полис", "task_id": "default"}],
        "responses": {}, "background": [], "context": {"secret_for_main": "PRIVATE"},
        "retrieval": [], "last_seq": 1,
    }
    ensure_blackboard(value)
    return value


def test_specialist_only_receives_declared_inputs():
    value = state()
    put_record(value, key="city", value="Almaty", task_id="default")
    put_record(value, key="private", value="PRIVATE", task_id="default")
    update_task(value, "claim", title="Claim")
    put_record(value, key="city", value="Astana", task_id="claim")
    view = package(value, agent={"reads": ["city"]}, task_id="default")
    assert view["user_text"] == "" and view["history"] == [] and view["context"] == {}
    assert [(r["key"], r["value"]) for r in view["blackboard"]] == [("city", "Almaty")]
    assert "PRIVATE" not in json.dumps(view)
    assert "Astana" not in json.dumps(view)


def test_compaction_keeps_delivery_and_marks_omissions():
    value = state()
    turns = []
    for turn in range(1, 30):
        turns.extend([
            {"turn_id": turn, "role": "user", "text": "Вопрос " * 1000},
            {"turn_id": turn, "role": "assistant", "text": "NO_DUPLICATE",
             "segments": [{"text": "Ответ " * 1000, "delivery": {"status": "unheard"}}]},
        ])
    value["conversation_history"] = turns
    value["messages"] = [{"turn_id": 30, "text": "CURRENT INPUT", "task_id": "default"}]
    value["responses"] = {"r": {"turn_id": 30, "task_id": "default", "segments": [{"text": "FIXED PREFIX"}]}}
    before = deepcopy(value)
    result = package(value, "r")
    assert result["history_truncated"] and result["history_summary"]
    assert result["user_text"] == "CURRENT INPUT" and result["prefix"] == "FIXED PREFIX"
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= MAX_PACKAGE_CHARS
    assert any(s["delivery"]["status"] == "unheard" for h in result["history"]
               for s in h.get("segments", []))
    assert "NO_DUPLICATE" not in json.dumps(result)
    assert value == before


def test_task_sources_do_not_enter_another_tasks_package():
    value = state()
    update_task(value, "claim", title="Claim")
    value["retrieval"] = [
        {"task_id": "claim", "generation": 1, "input_revision": 2,
         "result": {"text": "OTHER_TASK_SOURCE"}},
        {"task_id": "default", "generation": 1, "input_revision": 2,
         "result": {"text": "CURRENT_SOURCE"}},
    ]
    result = package(value, task_id="default")
    assert result["sources"] == [{"text": "CURRENT_SOURCE"}]
    assert "OTHER_TASK_SOURCE" not in json.dumps(result)


def test_blackboard_tool_argument_boundary():
    assert _validated_arguments("blackboard_read", '{"keys":["city"]}', {"blackboard_read"}) == {"keys": ["city"]}
    with pytest.raises(ValueError):
        _validated_arguments("blackboard_read", '{"keys":["city"],"task_id":"other"}', {"blackboard_read"})


def test_call_briefs_are_task_scoped():
    value = state()
    update_task(value, "claim")
    value["context"]["call_brief"] = {"instruction": "LEGACY_DEFAULT"}
    value["task_contexts"] = {"claim": {"call_brief": {"instruction": "CLAIM_ONLY"}}}
    assert package(value, task_id="claim")["context"]["call_brief"]["instruction"] == "CLAIM_ONLY"
    assert "CLAIM_ONLY" not in json.dumps(package(value, task_id="default"))


def test_narrow_dag_reader_receives_parent_record():
    value = state()
    put_record(value, key="agent:parent", value={"summary": "PARENT_RESULT"},
               task_id="default", source="agent")
    view = package(value, agent={"reads": ["city"], "depends_on": ["parent"]})
    assert [r["key"] for r in view["blackboard"]] == ["agent:parent"]
    assert "PARENT_RESULT" in json.dumps(view)
