"""Canonical conversation history for domain adapters and text/voice runtimes."""

from copy import deepcopy

from app.context.types import SessionContext


def delivery(segment: dict, response: dict) -> dict:
    if segment["segment_id"] in response.get("text_segment_ids", []):
        return {"status": "heard", "mode": "text"}
    timing = response.get("timeline", {}).get(segment["segment_id"])
    played = response.get("played_ms")
    if played is None:
        return {"status": "unknown"}
    if played == 0:
        return {"status": "unheard", "offset_ms": 0}
    if timing is None:
        return {"status": "unknown", "response_played_ms": played}
    if played >= timing["end_ms"]:
        return {"status": "heard", **timing}
    if played <= timing["start_ms"]:
        return {"status": "unheard", "offset_ms": 0, **timing}
    return {"status": "partially_heard", "offset_ms": played - timing["start_ms"], **timing}


def _response_delivery(segments: list[dict]) -> dict:
    statuses = {segment["delivery"]["status"] for segment in segments}
    if len(statuses) == 1:
        status = next(iter(statuses))
        modes = {segment["delivery"].get("mode") for segment in segments}
        return {"status": status, **({"mode": "text"} if modes == {"text"} else {})}
    if "partially_heard" in statuses or ("heard" in statuses and "unheard" in statuses):
        return {"status": "partially_heard"}
    return {"status": "unknown"}


def kernel_history(state: dict) -> list[dict]:
    """Full generated segments survive interruption; ACK alone determines delivery."""
    output = []
    for message in state.get("messages", []):
        response = state.get("responses", {}).get(message.get("response_id"))
        task_id = message.get("task_id", (response or {}).get("task_id", "default"))
        output.append({
            "role": "user", "turn_id": message["turn_id"], "text": message["text"],
            "task_id": task_id,
        })
        if response is None:
            continue
        segments = [
            {**deepcopy(segment), "delivery": delivery(segment, response)}
            for segment in response.get("segments", [])
        ]
        output.append({
            "role": "assistant", "turn_id": message["turn_id"], "task_id": task_id,
            "response_id": message["response_id"], "status": response["status"],
            "text": "".join(segment["text"] for segment in segments),
            "played_ms": response.get("played_ms"), "segments": segments,
            "delivery": _response_delivery(segments),
        })
    return output


def conversation_history(context: SessionContext) -> list[dict]:
    """Merge by turn/role. A runtime response is authoritative for its immutable segments."""
    merged = {}
    current_kernel = context.kernel if context.kernel.get("generation", context.generation) == context.generation else {}
    turn_tasks = current_kernel.get("turn_tasks", {})
    for turn in context.history:
        role = "user" if turn.role == "client" else "assistant"
        item = {
            "role": role, "turn_id": turn.turn_id, "text": turn.text,
            "language": turn.language, "task_id": turn_tasks.get(str(turn.turn_id)),
        }
        if role == "assistant":
            item.update({
                "status": "completed", "delivery": {"status": "unknown"},
                "segments": [{
                    "segment_id": f"domain:{context.generation}:{turn.turn_id}",
                    "index": 0, "text": turn.text, "status": "completed",
                    "delivery": {"status": "unknown"}, "used_source_ids": [],
                }],
            })
        merged[(turn.turn_id, role)] = item
    for item in kernel_history(current_kernel):
        identity = (item["turn_id"], item["role"])
        if (item["role"] == "assistant" and not item["text"]
                and identity in merged and merged[identity]["text"]):
            # A failed empty runtime attempt must not hide a subsequently supplied template reply.
            merged[identity]["task_id"] = item["task_id"]
            continue
        if identity in merged and merged[identity].get("language"):
            item["language"] = merged[identity]["language"]
        merged[identity] = item
    return [merged[identity] for identity in sorted(
        merged, key=lambda identity: (identity[0], identity[1] != "user")
    )]
