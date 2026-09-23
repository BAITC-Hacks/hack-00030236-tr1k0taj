"""Request ledger and public call replay, written through the Contexts owner."""

import hashlib
import json
from copy import deepcopy

from pydantic import TypeAdapter

from app.call.events import TurnEvent
from app.kernel import KernelError

_EVENT = TypeAdapter(TurnEvent)
TERMINAL = {"completed", "cancelled", "failed"}


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


class CallJournal:
    def __init__(self, contexts):
        self.contexts = contexts

    async def state(self, sid):
        context = await self.contexts.snapshot(sid)
        return context.generation, context.call_journal

    async def lookup(self, sid, request_id, digest=None):
        generation, journal = await self.state(sid)
        request = journal.get("requests", {}).get(request_id)
        if request is not None:
            if request["generation"] != generation:
                raise KernelError("request_generation_conflict", "Запрос принадлежит прошлому звонку")
            if digest is not None and request["fingerprint"] != digest:
                raise KernelError("request_conflict", "request_id уже использован с другим телом")
        return deepcopy(request)

    async def begin(self, sid, request_id, digest):
        async with self.contexts.mutate(sid, author="system") as mutation:
            journal = mutation.ctx.call_journal
            requests = journal.setdefault("requests", {})
            if existing := requests.get(request_id):
                if existing["generation"] != mutation.ctx.generation:
                    raise KernelError("request_generation_conflict", "Запрос принадлежит прошлому звонку")
                if existing["fingerprint"] != digest:
                    raise KernelError("request_conflict", "request_id уже использован с другим телом")
                return {"is_new": False, **deepcopy(existing)}
            if len(requests) >= 100:
                raise KernelError("call_request_limit", "Создайте сессию с новым UUID", 429)
            record = {"fingerprint": digest, "generation": mutation.ctx.generation,
                      "status": "running", "turn_id": 0,
                      "first_seq": journal.get("seq", 0) + 1,
                      "last_seq": journal.get("seq", 0), "bytes": 0}
            requests[request_id] = record
            return {"is_new": True, **deepcopy(record)}

    async def append(self, sid, request_id, event):
        # Only the call union is accepted: no raw kernel/agent/tool envelope can enter this stream.
        public = _EVENT.validate_python(event).model_dump(mode="json")
        async with self.contexts.mutate(sid, author="system") as mutation:
            journal = mutation.ctx.call_journal
            record = journal["requests"][request_id]
            if record["generation"] != mutation.ctx.generation or record["status"] in TERMINAL:
                return None
            size = len(json.dumps(public, ensure_ascii=False).encode())
            terminal = public["type"] in {"turn.done", "turn.cancelled"} or (
                public["type"] == "error" and public["fatal"]
            )
            if not terminal and (size > 4 * 1024 * 1024
                    or record.get("bytes", 0) + size > 16 * 1024 * 1024
                    or journal.get("bytes", 0) + size > 64 * 1024 * 1024):
                raise KernelError("call_replay_limit", "Превышен размер сохраняемого ответа", 429)
            record["bytes"] = record.get("bytes", 0) + size
            journal["bytes"] = journal.get("bytes", 0) + size
            sequence = journal.get("seq", 0) + 1
            envelope = {**public, "event_seq": sequence, "request_id": request_id,
                        "generation": mutation.ctx.generation}
            journal["seq"] = record["last_seq"] = sequence
            if envelope["turn_id"]:
                record["turn_id"] = envelope["turn_id"]
            if envelope["type"] == "turn.done":
                record["status"] = "completed"
            elif envelope["type"] == "turn.cancelled":
                record["status"] = "cancelled"
            elif envelope["type"] == "error" and envelope["fatal"]:
                record["status"] = "failed"
            mutation.entry("call", {"stream_event": envelope}, turn_id=envelope["turn_id"])
            return envelope

    async def finish(self, sid, request_id, status):
        async with self.contexts.mutate(sid, author="system") as mutation:
            record = mutation.ctx.call_journal["requests"][request_id]
            if record["status"] not in TERMINAL:
                record["status"] = status

    async def validate_cursor(self, sid, after, request_id=None):
        _, journal = await self.state(sid)
        if after < 0 or after > journal.get("seq", 0):
            raise KernelError("invalid_cursor", "Курсор вне границ журнала звонка", 422)
        if request_id is not None and await self.lookup(sid, request_id) is None:
            raise KernelError("request_not_found", "Запрос не найден", 404)

    async def events(self, sid, after=0, limit=200, request_id=None):
        generation, _ = await self.state(sid)
        output = []
        for entry in await self.contexts.board(sid):
            item = entry.payload.get("stream_event") if entry.type == "call" else None
            if (item is not None and item["generation"] == generation
                    and item.get("event_seq", item.get("seq", 0)) > after
                    and (request_id is None or item["request_id"] == request_id)):
                output.append(item)
        return sorted(output, key=lambda item: item.get("event_seq", item.get("seq", 0)))[:limit]
