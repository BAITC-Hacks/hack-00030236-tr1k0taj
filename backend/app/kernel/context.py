"""Client delivery is explicit; generated text is never implicitly 'heard'."""

from copy import deepcopy


def delivery(segment, response):
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


def history(state):
    output = []
    for message in state["messages"]:
        response = state["responses"][message["response_id"]]
        output.append({"role": "user", "turn_id": message["turn_id"], "text": message["text"]})
        output.append({
            "role": "assistant", "turn_id": message["turn_id"],
            "response_id": message["response_id"], "status": response["status"],
            "played_ms": response.get("played_ms"),
            "segments": [{**deepcopy(segment), "delivery": delivery(segment, response)}
                         for segment in response["segments"]],
        })
    return output


def package(state, response_id=None):
    """Keep current input/prefix intact; independently bound older and background data."""
    messages = state["messages"]
    response = state["responses"].get(response_id, {})
    current_turn = response.get("turn_id", messages[-1]["turn_id"] if messages else None)
    previous = [item for item in history(state) if item["turn_id"] != current_turn][-12:]
    truncated = len(messages) > 7
    remaining = 16000
    selected = []
    for item in reversed(previous):
        item = deepcopy(item)
        texts = [item] if item["role"] == "user" else item["segments"]
        for part in texts:
            original = part["text"]
            part["text"] = original[:min(2000, max(remaining, 0))]
            remaining -= len(part["text"])
            truncated |= original != part["text"]
        selected.append(item)
    background = [deepcopy(result) for result in state["background"]
                  if result["input_revision"] == state["input_revision"]
                  and result["generation"] == state["generation"]]
    # Facts/source IDs stay together; limit whole results rather than stripping provenance.
    background = background[-8:]
    for result in background:
        result["summary"] = result.get("summary", "")[:4000]
        result["facts"] = result.get("facts", [])[:10]
    return {
        "session_id": state["session_id"], "generation": state["generation"],
        "input_revision": state["input_revision"], "context": state["context"],
        "user_text": next((m["text"] for m in reversed(messages)
                           if m["turn_id"] == current_turn), ""),
        "history": list(reversed(selected)), "history_truncated": truncated,
        "background": background,
        "sources": [item["result"] for item in state.get("retrieval", [])
                    if item["input_revision"] == state["input_revision"]][-6:],
        "prefix": "".join(s["text"] for s in response.get("segments", [])),
        "segment_index": len(response.get("segments", [])),
        "consumed_board_seq": state["last_seq"],
    }


def snapshot(state):
    return {key: state[key] for key in (
        "session_id", "generation", "input_revision", "status", "last_seq",
        "active_response_id", "mode",
    )} | {"agents": [a["agent_id"] for a in state["agents"]]}
