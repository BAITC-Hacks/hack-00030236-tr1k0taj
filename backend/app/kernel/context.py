"""Bounded per-agent views over the shared conversation and task blackboard."""

import json
import time
from copy import deepcopy

from app.context import (
    delivery,
    ensure_blackboard,
    fingerprint_matches,
    kernel_history,
    select_records,
)

MAX_PACKAGE_CHARS = 64000

__all__ = ["delivery", "history", "package", "snapshot"]


def history(state):
    return deepcopy(state["conversation_history"] if "conversation_history" in state else kernel_history(state))


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _current(state, item, task_id):
    if item.get("generation", state["generation"]) != state["generation"]:
        return False
    if item.get("task_id", "default") != task_id:
        return False
    if item.get("input_expires_at") is not None and item["input_expires_at"] <= time.time():
        return False
    if "read_versions" in item:
        return fingerprint_matches(state, task_id, item["read_versions"])
    return item.get("input_revision") == state["input_revision"]


def package(state, response_id=None, *, agent=None, task_id=None):
    """No shared transcript for specialists that declare a narrower dependency set.

    Sources are admitted atomically with their content. A clipped history segment keeps
    delivery metadata; current input/prefix and explicitly selected inputs never silently truncate.
    """
    board = ensure_blackboard(state)
    response = state["responses"].get(response_id, {})
    task_id = task_id or response.get("task_id") or board["active_task_id"] or "default"
    task = board["tasks"].get(task_id, {"task_id": task_id, "status": "active"})
    messages = [m for m in state["messages"] if m.get("task_id", "default") == task_id]
    current_turn = response.get("turn_id", messages[-1]["turn_id"] if messages else None)
    reads = list(agent.get("reads", ["$message"])) if agent else None
    if reads is not None:
        reads = list(dict.fromkeys([*reads, *(f"agent:{parent}" for parent in agent.get("depends_on", []))]))
    narrow = reads is not None and "$message" not in reads
    records = select_records(state, task_id=task_id, keys=reads if narrow else None)
    user_text = "" if narrow else next(
        (m["text"] for m in reversed(messages) if m["turn_id"] == current_turn), ""
    )
    shared_context = deepcopy(state.get("context", {}))
    # Old call projections predate explicit tasks and belong to the default conversation.
    if task_id != "default":
        shared_context.pop("call_brief", None)
    shared_context.update(deepcopy(state.get("task_contexts", {}).get(task_id, {})))
    result = {
        "session_id": state["session_id"], "generation": state["generation"],
        "input_revision": state["input_revision"], "task_id": task_id,
        "task": {k: v for k, v in task.items() if k in {"task_id", "title", "status", "version"}},
        "context": {} if narrow else shared_context,
        "user_text": user_text, "history": [], "history_summary": [],
        "history_truncated": False, "blackboard": [], "background": [], "sources": [],
        "prefix": "".join(s["text"] for s in response.get("segments", [])),
        "segment_index": len(response.get("segments", [])),
        "consumed_board_seq": state["last_seq"],
        "context_truncated": False, "omitted_records": 0,
        "blocking_incomplete": list(response.get("blocking_incomplete", [])),
    }
    if _size(result) > MAX_PACKAGE_CHARS:
        raise ValueError("mandatory_context_exceeds_budget")

    def admit(field, item):
        result[field].append(deepcopy(item))
        if _size(result) > MAX_PACKAGE_CHARS - 256:
            result[field].pop()
            result["context_truncated"] = True
            return False
        return True

    for record in records:
        if record["key"] == "$message" or (not narrow and record["key"].startswith("agent:")):
            continue
        if not admit("blackboard", record):
            if narrow:
                raise ValueError("agent_inputs_exceed_context_budget")
            result["omitted_records"] += 1

    # Explicit readers only see declared records; unrelated background/history isn't an input.
    if not narrow:
        active_ids = {r["record_id"] for r in select_records(state, task_id=task_id)}
        for item in state.get("background", [])[-32:]:
            if not item.get("accepted", True) or not _current(state, item, task_id):
                continue
            if item.get("record_id") and item["record_id"] not in active_ids:
                continue
            value = deepcopy(item)
            value["summary"] = value.get("summary", "")[:4000]
            value["facts"] = value.get("facts", [])[:10]
            if len(result["background"]) >= 8:
                result["background"].pop(0)
                result["context_truncated"] = True
            admit("background", value)
        for item in state.get("retrieval", [])[-12:]:
            if _current(state, item, task_id):
                if len(result["sources"]) >= 6:
                    result["sources"].pop(0)
                    result["context_truncated"] = True
                admit("sources", item["result"])

        previous = [item for item in history(state)
                    if item.get("turn_id") != current_turn
                    and (item.get("task_id") or "default") == task_id]
        result["history_truncated"] = len(previous) > 12
        remaining = 16000
        for item in reversed(previous[-12:]):
            item = deepcopy(item)
            parts = [item] if item["role"] == "user" else item.get("segments", [item])
            for part in parts:
                original = part.get("text", "")
                part["text"] = original[:min(2000, max(remaining, 0))]
                remaining -= len(part["text"])
                if part["text"] != original:
                    part["text_truncated"] = True
                    result["history_truncated"] = True
            # Avoid duplicating full assistant text alongside bounded segments.
            if item.get("segments"):
                item.pop("text", None)
            if not admit("history", item):
                result["history_truncated"] = True
        result["history"].reverse()
        # Extractive references, never a model-created replacement for authoritative facts.
        for item in previous[:-12][-6:]:
            if item["role"] == "user":
                admit("history_summary", {"turn_id": item["turn_id"], "kind": "user_excerpt",
                    "text": item.get("text", "")[:400], "truncated": len(item.get("text", "")) > 400})
    result["has_sources"] = bool(result["sources"] or result["background"])
    result["context_chars"] = _size(result)
    return result


def snapshot(state):
    return {key: state[key] for key in (
        "session_id", "generation", "input_revision", "status", "last_seq",
        "active_response_id", "mode",
    )} | {"agents": [a["agent_id"] for a in state["agents"]]}
