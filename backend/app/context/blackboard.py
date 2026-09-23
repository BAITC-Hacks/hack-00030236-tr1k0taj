"""Versioned working knowledge. Pure in-memory rules; Contexts owns persistence."""

import json
import math
import re
import time
from copy import deepcopy
from typing import Any, Literal, TypedDict
from uuid import uuid4

MAX_TASKS = 32
MAX_RECORDS = 2000
MAX_VALUE_CHARS = 16000
MAX_DEPENDENCIES = 32
_TASK_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$")


class BlackboardTask(TypedDict):
    task_id: str
    title: str
    status: Literal["active", "paused", "completed", "cancelled"]
    version: int


class BlackboardRecord(TypedDict):
    record_id: str
    key: str
    task_id: str | None
    value: Any
    source: Literal["user", "tool", "agent", "system"]
    source_id: str | None
    version: int
    status: Literal["active", "superseded", "stale"]
    supersedes: str | None
    depends_on: list[str]
    expires_at: float | None
    created_at: float


class Blackboard(TypedDict):
    schema_version: int
    version: int
    tasks: dict[str, BlackboardTask]
    records: dict[str, BlackboardRecord]
    heads: dict[str, str]
    active_task_id: str


def _task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ValueError("invalid_task_id")


def _key(key: str) -> None:
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise ValueError("invalid_record_key")


def _scope(task_id: str | None, key: str) -> str:
    return json.dumps([task_id, key], ensure_ascii=False, separators=(",", ":"))


def ensure_blackboard(state: dict) -> Blackboard:
    """Upgrade a legacy runtime projection without manufacturing any remembered facts."""
    if "blackboard" not in state:
        state["blackboard"] = {
            "schema_version": 2, "version": 0,
            "tasks": {"default": {
                "task_id": "default", "title": "Default", "status": "active", "version": 1,
            }},
            "records": {}, "heads": {}, "active_task_id": "default",
        }
    board = state["blackboard"]
    if (
        not isinstance(board, dict)
        or board.get("schema_version") != 2
        or type(board.get("version")) is not int
        or board["version"] < 0
        or not all(isinstance(board.get(name), dict) for name in ("tasks", "records", "heads"))
    ):
        raise ValueError("invalid_blackboard_schema")
    if len(board["tasks"]) > MAX_TASKS or len(board["records"]) > MAX_RECORDS:
        raise ValueError("blackboard_limit")
    return board


def _event(kind: str, payload: dict) -> dict:
    return {"type": kind, "author": "scheduler", "visibility": "internal", "payload": payload}


def update_task(
    state: dict, task_id: str, title: str | None = None,
    status: str | None = None, focus: bool = False,
) -> tuple[BlackboardTask, list[dict]]:
    _task_id(task_id)
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 500):
        raise ValueError("invalid_task_title")
    if status is not None and status not in ("active", "paused", "completed", "cancelled"):
        raise ValueError("invalid_task_status")
    if type(focus) is not bool:
        raise ValueError("invalid_task_focus")
    board = deepcopy(ensure_blackboard(state))
    before = board["tasks"].get(task_id)
    if before is None and len(board["tasks"]) >= MAX_TASKS:
        raise ValueError("task_limit")
    task = dict(before) if before else {
        "task_id": task_id, "title": title or task_id, "status": "active", "version": 1,
    }
    if title is not None:
        task["title"] = title
    if status is not None:
        task["status"] = status
    if focus and task["status"] != "active":
        raise ValueError("task_inactive")
    changed = before != task or (focus and board["active_task_id"] != task_id)
    if not changed:
        return deepcopy(task), []
    if before is not None:
        task["version"] += 1
    board["tasks"][task_id] = task
    if focus:
        board["active_task_id"] = task_id
    board["version"] += 1
    state["blackboard"] = board
    return deepcopy(task), [_event("task.updated", {**task, "focus": focus})]


def _current(board: Blackboard, record_id: str, now: float) -> bool:
    # Iterative DFS: long but bounded dependency chains cannot overflow Python's call stack.
    pending, visiting, checked = [(record_id, False)], set(), set()
    while pending:
        identity, exiting = pending.pop()
        if exiting:
            visiting.remove(identity)
            checked.add(identity)
            continue
        if identity in checked:
            continue
        record = board["records"].get(identity)
        if record is None or identity in visiting or record["status"] != "active":
            return False
        if board["heads"].get(_scope(record["task_id"], record["key"])) != identity:
            return False
        if record["expires_at"] is not None and record["expires_at"] <= now:
            return False
        if record["task_id"] is not None:
            task = board["tasks"].get(record["task_id"])
            if task is None or task["status"] in ("completed", "cancelled"):
                return False
        visiting.add(identity)
        pending.append((identity, True))
        pending.extend((parent, False) for parent in record["depends_on"])
    return True


def put_record(
    state: dict, *, key: str, value: Any, task_id: str | None = None,
    source: str = "user", source_id: str | None = None, depends_on: list[str] | None = None,
    expires_at: float | None = None, expected_record_id: str | None = None,
) -> tuple[BlackboardRecord, list[dict]]:
    _key(key)
    if task_id is not None:
        _task_id(task_id)
    if key == "$message" and task_id is None:
        raise ValueError("message_requires_task_scope")
    if source not in ("user", "tool", "agent", "system"):
        raise ValueError("invalid_record_source")
    if source_id is not None and (not isinstance(source_id, str) or len(source_id) > 500):
        raise ValueError("invalid_source_id")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid_record_value") from exc
    if len(encoded) > MAX_VALUE_CHARS:
        raise ValueError("record_value_limit")
    if expires_at is not None and (
        type(expires_at) not in (int, float) or not math.isfinite(expires_at)
    ):
        raise ValueError("invalid_expiry")
    dependencies = depends_on if depends_on is not None else []
    if (not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES
            or any(not isinstance(item, str) for item in dependencies)):
        raise ValueError("invalid_record_dependencies")
    dependencies = list(dict.fromkeys(dependencies))
    board = deepcopy(ensure_blackboard(state))
    if task_id is not None:
        task = board["tasks"].get(task_id)
        if task is None:
            raise ValueError("task_not_found")
        if task["status"] in ("completed", "cancelled"):
            raise ValueError("task_inactive")
    if len(board["records"]) >= MAX_RECORDS:
        raise ValueError("record_limit")
    head = board["heads"].get(_scope(task_id, key))
    if expected_record_id is not None and expected_record_id != head:
        raise ValueError("record_version_conflict")
    now = time.time()
    for dependency in dependencies:
        parent = board["records"].get(dependency)
        if parent is None:
            raise ValueError("dependency_not_found")
        if parent["task_id"] not in (None, task_id):
            raise ValueError("dependency_task_mismatch")
        if dependency == head or not _current(board, dependency, now):
            raise ValueError("dependency_not_current")
    previous = board["records"].get(head)
    record = {
        "record_id": str(uuid4()), "key": key, "task_id": task_id, "value": deepcopy(value),
        "source": source, "source_id": source_id,
        "version": previous["version"] + 1 if previous else 1,
        "status": "active", "supersedes": head, "depends_on": dependencies,
        "expires_at": expires_at, "created_at": now,
    }
    invalidated = []
    if previous is not None:
        previous["status"] = "superseded"
        invalidated.append(previous["record_id"])
        # Iterative propagation also marks descendants several levels below the replaced input.
        changed = True
        while changed:
            changed = False
            for candidate in board["records"].values():
                if candidate["status"] == "active" and any(
                    dep in invalidated for dep in candidate["depends_on"]
                ):
                    candidate["status"] = "stale"
                    invalidated.append(candidate["record_id"])
                    changed = True
    if any(dependency in invalidated for dependency in dependencies):
        raise ValueError("dependency_not_current")
    board["records"][record["record_id"]] = record
    board["heads"][_scope(task_id, key)] = record["record_id"]
    board["version"] += 1
    state["blackboard"] = board
    return deepcopy(record), [_event("blackboard.updated", {
        "record": deepcopy(record), "invalidated_record_ids": invalidated,
        "board_version": board["version"],
    })]


def select_records(
    state: dict, task_id: str | None = None, keys: list[str] | None = None,
    now: float | None = None,
) -> list[BlackboardRecord]:
    board = ensure_blackboard(state)
    if task_id is not None:
        _task_id(task_id)
        if task_id not in board["tasks"]:
            raise ValueError("task_not_found")
    if keys is not None:
        if not isinstance(keys, list) or len(keys) > 64:
            raise ValueError("invalid_read_keys")
        for key in keys:
            _key(key)
    chosen = None if keys is None else set(keys)
    clock = time.time() if now is None else now
    selected = {}
    # Task-local records take precedence over shared records for the same key.
    for scope in ([None] if task_id is None else [None, task_id]):
        for record in board["records"].values():
            if (record["task_id"] == scope and (chosen is None or record["key"] in chosen)
                    and _current(board, record["record_id"], clock)):
                selected[record["key"]] = record
    return [deepcopy(selected[key]) for key in sorted(selected)]


def input_versions(state: dict, task_id: str | None, keys: list[str]) -> dict[str, str | None]:
    if not isinstance(keys, list) or len(keys) > 64:
        raise ValueError("invalid_read_keys")
    for key in keys:
        _key(key)
    records = {record["key"]: record for record in select_records(state, task_id, keys)}
    return {
        key: records[key]["record_id"] if key in records and (
            key != "$message" or records[key]["task_id"] == task_id
        ) else None
        for key in keys
    }


def fingerprint_matches(state: dict, task_id: str | None, fingerprint: dict) -> bool:
    board = ensure_blackboard(state)
    if task_id is not None and board["tasks"].get(task_id, {}).get("status") != "active":
        return False
    if not isinstance(fingerprint, dict):
        return False
    return input_versions(state, task_id, list(fingerprint)) == fingerprint
