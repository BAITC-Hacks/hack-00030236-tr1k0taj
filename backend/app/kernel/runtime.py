"""One foreground writer, bounded background DAG, persisted public/private event streams."""

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import asdict
from uuid import uuid4

from opentelemetry import trace

from app.config import settings
from app.context import (
    ensure_blackboard,
    fingerprint_matches,
    input_versions,
    put_record,
    select_records,
    update_task,
)
from app.kernel.context import package, snapshot
from app.kernel.provider import ProviderError
from app.kernel.store import KernelError, Repository, event
from app.kernel.types import (
    BlackboardReadArgs,
    CreateSession,
    InterruptRequest,
    PlaybackRequest,
    RAGReadArgs,
    RAGSearchArgs,
    RecordUpdate,
    TurnRequest,
)
from app.knowledge import open_knowledge
from app.tracer import current_span_context, span

log = logging.getLogger(__name__)


def request_key(kind, payload):
    return hashlib.sha256(json.dumps([kind, payload], sort_keys=True).encode()).hexdigest()


def canonical_updates(updates, task_id):
    return [
        {**update.model_dump(mode="json"),
         "task_id": update.task_id if "task_id" in update.model_fields_set else task_id}
        for update in updates
    ]


def replay(state, rid, digest):
    prior = state["requests"].get(rid)
    if prior and prior["digest"] != digest:
        raise KernelError("request_conflict", "request_id уже использован с другими параметрами")
    return prior


def tool_trace(sid, turn_id, name, args, result):
    """Атрибуты span'а инструмента (tracer-module.md): без сырого запроса вне `local.*`."""
    if name == "blackboard_read":
        return {"session.id": sid, "turn.id": turn_id,
                "blackboard.records": len(result.get("records", [])),
                "blackboard.found": "error" not in result}
    if name != "rag_search":
        return {"session.id": sid, "turn.id": turn_id, "kb.kind": args["kind"],
                "kb.key": args["key"], "kb.found": "error" not in result}
    hits = result.get("hits", [])
    return {"session.id": sid, "turn.id": turn_id, "rag.kinds": args["kinds"],
            "rag.limit": args["limit"], "rag.search_mode": result.get("search_mode"),
            "rag.degraded_reason": result.get("degraded_reason") or result.get("error"),
            "rag.hits": len(hits), "rag.source_ids": json.dumps([h.get("source_id") for h in hits]),
            "local.rag.query": args["query"], "local.rag.search_query": args.get("search_query")}


def require_open(state):
    if state["status"] != "open":
        raise KernelError("session_closed", "Сессия завершена")


def require_response(state, response_id):
    response = state["responses"].get(response_id)
    if response is None:
        raise KernelError("response_not_found", "Ответ не принадлежит этой сессии", 404)
    return response


class Runtime:
    def __init__(self, driver, repository=None, tool_executor=None):
        self.driver = driver
        self.repo = repository or Repository()
        self.tool_executor = tool_executor or self._knowledge_tool
        self.locks = defaultdict(asyncio.Lock)
        self.signals = defaultdict(asyncio.Event)
        self.main_tasks = {}
        self.background_tasks = defaultdict(dict)
        self.tool_tasks = {}
        self.tool_owners = defaultdict(list)
        self.retired_tasks = set()
        self.trace_links = {}  # sid → span хода, в памяти: link для фоновых agent.run
        self.semaphores = defaultdict(lambda: asyncio.Semaphore(settings.kernel_background_parallelism))
        self.global_semaphore = asyncio.Semaphore(settings.kernel_global_parallelism)
        # Reserve model capacity for foreground work even when many sessions have queued background work.
        self.background_capacity = asyncio.Semaphore(max(1, settings.kernel_global_parallelism - 1))
        if hasattr(self.repo, "contexts"):
            self.repo.contexts.on_reset(self.cancel_generation)

    def cancel_generation(self, sid, generation):
        """Contexts already committed the reset; cancel old work without awaiting under its lock."""
        if task := self.main_tasks.get(sid):
            task.cancel()
        for task in self.background_tasks[sid].values():
            task.cancel()
        for key, task in list(self.tool_tasks.items()):
            if key[0] == sid:
                task.cancel()
                self.tool_tasks.pop(key)
        self.signals[sid].set()

    async def stream_turn(self, sid, turn_id, transcript, brief, *, request_id=None,
                          task_id="default", updates=None, interrupt_previous=False):
        """Use the existing call UUID/turn; stream only this response's public events."""
        request = CreateSession()
        state = await self.repo.create(
            [a.model_dump() for a in request.agents], {},
            "mock" if settings.mock_mode else "live", session_id=sid,
        )
        # A retried ingress must replay the saved response, including its earlier deltas.
        cursor = 0 if request_id and str(request_id) in state["requests"] else state["last_seq"]
        accepted = await self.submit(
            sid, TurnRequest(request_id=request_id or uuid4(), text=transcript,
                             task_id=task_id, updates=updates or [],
                             interrupt_previous=interrupt_previous),
            turn_id=turn_id, brief=brief,
        )
        rid = accepted["response_id"]
        try:
            while True:
                self.signals[sid].clear()
                batch = await self.repo.events(sid, cursor)
                for item in batch:
                    cursor = item["seq"]
                    if item.get("response_id") != rid:
                        continue
                    yield item
                    if item["type"] in {
                        "response.completed", "response.interrupted", "response.failed",
                    }:
                        return
                if len(batch) == 200:
                    continue
                current = await self.repo.get(sid)
                response = current["responses"].get(rid)
                if current["status"] == "closed" or response is None:
                    return
                if response["status"] != "generating":
                    # Drain all pages until the terminal event, even if a commit raced the query.
                    continue
                try:
                    await asyncio.wait_for(self.signals[sid].wait(), 1)
                except TimeoutError:
                    pass
        finally:
            await self.interrupt_turn(sid, turn_id, generating_only=True)

    async def interrupt_turn(self, sid, turn_id, played_ms=None, *, generating_only=False):
        try:
            state = await self.repo.get(sid)
        except KernelError as exc:
            if exc.status == 404:
                return
            raise
        response = next((r for r in reversed(list(state["responses"].values()))
                         if r["turn_id"] == turn_id), None)
        if response is None or (generating_only and response["status"] != "generating"):
            return
        if played_ms is not None:
            return await self.playback(sid, InterruptRequest(
                request_id=uuid4(), response_id=response["response_id"], played_ms=played_ms,
            ), interrupt=True)
        async with self.locks[sid]:
            def apply(state, seq):
                saved = state["responses"].get(response["response_id"])
                if saved is None or saved["status"] == "interrupted":
                    return False, []
                saved["status"] = "interrupted"
                for segment in saved["segments"]:
                    if segment["status"] == "generating":
                        segment["status"] = "interrupted"
                # A disconnect is not a playback acknowledgement: preserve unknown delivery.
                return state["active_response_id"] == saved["response_id"], [event("response.interrupted", {"played_ms": saved["played_ms"]},
                    author="user", public=True, response_id=saved["response_id"], turn_id=turn_id)]
            cancel = await self._change(sid, apply)
            if cancel and (task := self.main_tasks.get(sid)):
                task.cancel()

    async def playback_turn(self, sid, turn_id, body):
        state = await self.repo.get(sid)
        rid = body.get("response_id")
        if rid is None:
            rid = next((r["response_id"] for r in state["responses"].values()
                        if r["turn_id"] == turn_id), None)
        response = require_response(state, str(rid))
        if response["turn_id"] != turn_id:
            raise KernelError("response_not_found", "Ответ не принадлежит этому ходу", 404)
        fields = {key: value for key, value in body.items() if key in PlaybackRequest.model_fields}
        fields.setdefault("request_id", uuid4())
        fields["response_id"] = response["response_id"]
        return await self.playback(sid, PlaybackRequest.model_validate(fields))

    async def _change(self, sid, apply, *, durable=True):
        try:
            result, events = await self.repo.change(sid, apply, durable=durable)
        except ValueError as exc:
            code = "record_version_conflict" if str(exc) == "record_version_conflict" else "invalid_blackboard"
            raise KernelError(code, str(exc), 409 if code == "record_version_conflict" else 422) from exc
        if events:
            self.signals[sid].set()
        return result

    @staticmethod
    def _interrupt_response(response, *, author="user"):
        response["status"] = "interrupted"
        for segment in response["segments"]:
            if segment["status"] == "generating":
                segment["status"] = "interrupted"
        return event("response.interrupted", {"played_ms": response.get("played_ms")},
                     author=author, public=True, response_id=response["response_id"],
                     turn_id=response["turn_id"])

    def _invalidate_runs(self, state):
        events = []
        for run in state["runs"].values():
            if run["status"] == "completed" and "read_versions" in run:
                versions = input_versions(state, run.get("task_id", "default"),
                                          list(run["read_versions"]))
                if versions != run["read_versions"]:
                    run["status"] = "stale"
                    for result in state["background"]:
                        if result["run_id"] == run["run_id"]:
                            record = ensure_blackboard(state)["records"].get(result.get("record_id"))
                            if record and record["status"] == "active":
                                record["status"] = "stale"
                                events.append(event("record.invalidated", {
                                    "record_id": record["record_id"], "task_id": record["task_id"],
                                }))
                    events.append(event("agent.stale", dict(run), author=run["agent_id"],
                                        turn_id=run["turn_id"]))
            if run["status"] in ("queued", "running") and not self._fresh(state, run):
                run["status"] = "stale"
                events.append(event("agent.stale", dict(run), author=run["agent_id"],
                                    turn_id=run["turn_id"]))
        return events

    @staticmethod
    def _refresh_messages(state, scopes, *, skip=None):
        """Broad readers depend on the message epoch, including explicit fact corrections."""
        board = ensure_blackboard(state)
        affected = list(board["tasks"]) if None in scopes else list(scopes)
        pending = []
        for scope in affected:
            if scope == skip or board["tasks"].get(scope, {}).get("status") not in ("active", "paused"):
                continue
            current = select_records(state, task_id=scope, keys=["$message"])
            if current:
                _, emitted = put_record(state, key="$message", value=current[0]["value"],
                                        task_id=scope, source="user")
                pending.extend(emitted)
        return pending

    @staticmethod
    def _input_expiry(state, scope, agent):
        reads = list(dict.fromkeys([*agent.get("reads", ["$message"]),
                                   *(f"agent:{parent}" for parent in agent.get("depends_on", []))]))
        records = select_records(state, task_id=scope, keys=None if "$message" in reads else reads)
        board = ensure_blackboard(state)
        pending = [record["record_id"] for record in records]
        seen, expiries = set(), []
        while pending:
            record_id = pending.pop()
            if record_id in seen:
                continue
            seen.add(record_id)
            record = board["records"][record_id]
            if record.get("expires_at") is not None:
                expiries.append(record["expires_at"])
            pending.extend(record["depends_on"])
        return min(expiries) if expiries else None

    def _cancel_task(self, task):
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()
            self.retired_tasks.add(task)
            task.add_done_callback(self.retired_tasks.discard)

    def _cancel_invalid_work(self, sid, state):
        for run_id, task in self.background_tasks[sid].items():
            run = state["runs"].get(run_id)
            if run is None or not self._fresh(state, run):
                self._cancel_task(task)
        # Results are cached by task/fingerprint; unrelated background requests survive new input.
        for key, task in list(self.tool_tasks.items()):
            if key[0] != sid:
                continue
            if not any(self._fresh(state, owner) for owner in self.tool_owners[key]):
                self._cancel_task(task)
                self.tool_tasks.pop(key)
                self.tool_owners.pop(key, None)

    async def create(self, request):
        state = await self.repo.create(
            [a.model_dump() for a in request.agents], request.context,
            "mock" if settings.mock_mode else "live",
            session_id=str(request.session_id) if request.session_id else None,
        )
        return snapshot(state)

    async def recover(self):
        for sid in await self.repo.open_sessions():
            await self.close_session(sid, "server_restart")

    async def _control(self, sid, kind, request_id, payload, mutate):
        digest = request_key(kind, payload)
        req_id = str(request_id)
        async with self.locks[sid]:
            def apply(state, seq):
                if prior := replay(state, req_id, digest):
                    return prior["result"], []
                require_open(state)
                result, pending = mutate(state)
                pending.extend(self._invalidate_runs(state))
                state["requests"][req_id] = {"digest": digest, "result": result}
                return result, pending
            result = await self._change(sid, apply)
            state = await self.repo.get(sid)
            self._cancel_invalid_work(sid, state)
            active = state["responses"].get(state["active_response_id"], {})
            if active.get("status") == "interrupted":
                self._cancel_task(self.main_tasks.get(sid))
            return result

    async def update_task(self, sid, request):
        def mutate(state):
            task, pending = update_task(state, request.task_id, title=request.title,
                                        status=request.status, focus=request.focus and request.status not in (
                                            "paused", "completed", "cancelled"
                                        ))
            response = state["responses"].get(state["active_response_id"], {})
            if (task["status"] != "active" and response.get("status") == "generating"
                    and response.get("task_id", "default") == request.task_id):
                pending.append(self._interrupt_response(response))
            return {"task": dict(task), "active_task_id": ensure_blackboard(state)["active_task_id"]}, pending
        return await self._control(sid, "task", request.request_id,
                                   request.model_dump(mode="json"), mutate)

    async def list_tasks(self, sid):
        board = ensure_blackboard(await self.repo.get(sid))
        return {"tasks": list(board["tasks"].values()), "active_task_id": board["active_task_id"]}

    @staticmethod
    def _record_metadata(record):
        return {key: value for key, value in record.items() if key != "value"}

    async def update_record(self, sid, request):
        def mutate(state):
            record, pending = put_record(state, **request.model_dump(exclude={"request_id"}))
            if request.key != "$message":
                pending.extend(self._refresh_messages(state, [request.task_id]))
            return {"record": self._record_metadata(record)}, pending
        return await self._control(sid, "record", request.request_id,
                                   request.model_dump(mode="json"), mutate)

    async def list_records(self, sid, task_id=None):
        state = await self.repo.get(sid)
        board = ensure_blackboard(state)
        scope = task_id or board["active_task_id"]
        if scope not in board["tasks"]:
            raise KernelError("task_not_found", "Задача не найдена", 404)
        current = {record["record_id"] for record in select_records(state, task_id=scope)}
        return {"task_id": scope, "records": [
            {**self._record_metadata(record), "active": record["record_id"] in current}
            for record in board["records"].values() if record.get("task_id") in (None, scope)
        ]}

    async def cancel_background(self, sid, request):
        def mutate(state):
            board = ensure_blackboard(state)
            scope = request.task_id or board["active_task_id"]
            if scope not in board["tasks"]:
                raise KernelError("task_not_found", "Задача не найдена", 404)
            cancelled, pending = [], []
            for run in state["runs"].values():
                if run.get("task_id", "default") == scope and run["status"] in ("queued", "running"):
                    run["status"] = "cancelled"
                    cancelled.append(run["run_id"])
                    pending.append(event("agent.cancelled", dict(run), author=run["agent_id"],
                                         turn_id=run["turn_id"]))
            return {"task_id": scope, "cancelled_run_ids": cancelled}, pending
        return await self._control(sid, "background.cancel", request.request_id,
                                   request.model_dump(mode="json"), mutate)

    async def update_inputs(self, sid, *, request_id, task_id="default", updates=None, text=None,
                            turn_id=None):
        """Record a call's non-generating ingress without starting background work or speech."""
        default = CreateSession()
        await self.repo.create([a.model_dump() for a in default.agents], {},
                               "mock" if settings.mock_mode else "live", session_id=sid)
        parsed = [value if isinstance(value, RecordUpdate) else RecordUpdate.model_validate(value)
                  for value in updates or []]
        payload = {"task_id": task_id, "text": text, "turn_id": turn_id,
                   "updates": canonical_updates(parsed, task_id)}
        def mutate(state):
            board = ensure_blackboard(state)
            if board["tasks"].get(task_id, {}).get("status", "active") != "active":
                raise KernelError("task_inactive", "Сначала возобновите задачу")
            _, pending = update_task(state, task_id, focus=True)
            if turn_id is not None:
                state.setdefault("turn_tasks", {})[str(turn_id)] = task_id
            record_ids = []
            scopes = set()
            for update in parsed:
                fields = update.model_dump()
                if "task_id" not in update.model_fields_set:
                    fields["task_id"] = task_id
                record, emitted = put_record(state, **fields)
                scopes.add(fields["task_id"])
                record_ids.append(record["record_id"])
                pending.extend(emitted)
            pending.extend(self._refresh_messages(state, scopes, skip=task_id if text is not None else None))
            if text is not None:
                record, emitted = put_record(state, key="$message", value=text,
                                             task_id=task_id, source="user")
                record_ids.append(record["record_id"])
                pending.extend(emitted)
            return {"task_id": task_id, "record_ids": record_ids}, pending
        return await self._control(sid, "inputs", request_id, payload, mutate)

    async def submit(self, sid, request, *, turn_id=None, brief=None):
        payload = request.model_dump(mode="json")
        payload["updates"] = canonical_updates(request.updates, request.task_id)
        req_id, digest = str(request.request_id), request_key("turn", payload)
        async with self.locks[sid]:
            def apply(state, seq):
                if prior := replay(state, req_id, digest):
                    return (prior["result"], False), []
                require_open(state)
                if brief is not None and brief.get("generation", state["generation"]) != state["generation"]:
                    raise KernelError("stale_context", "Контекст звонка уже изменился")
                if not settings.mock_mode and not settings.openai_api_key:
                    raise KernelError("provider_not_configured", "Не настроен OPENAI_API_KEY", 503)
                active = state["responses"].get(state["active_response_id"], {})
                if active.get("status") == "generating" and not request.interrupt_previous:
                    raise KernelError("response_active", "Сначала прервите текущий ответ")
                if len(state["messages"]) >= 100:
                    raise KernelError("session_limit", "Создайте новую сессию после 100 реплик")
                board = ensure_blackboard(state)
                task = board["tasks"].get(request.task_id)
                if task is not None and task["status"] != "active":
                    raise KernelError("task_inactive", "Сначала возобновите задачу")
                _, pending = update_task(state, request.task_id, focus=True)
                scopes = set()
                for update in request.updates:
                    fields = update.model_dump()
                    # An update omitted from the ingress scope belongs to this turn's task.
                    if "task_id" not in update.model_fields_set:
                        fields["task_id"] = request.task_id
                    _, emitted = put_record(state, **fields)
                    scopes.add(fields["task_id"])
                    pending.extend(emitted)
                pending.extend(self._refresh_messages(state, scopes, skip=request.task_id))
                _, emitted = put_record(state, key="$message", value=request.text,
                                        task_id=request.task_id, source="user")
                pending.extend(emitted)
                if active.get("status") == "generating":
                    pending.append(self._interrupt_response(active))
                state["input_revision"] += 1
                turn, response_id = turn_id or state["next_turn_id"], str(uuid4())
                state.setdefault("turn_tasks", {})[str(turn)] = request.task_id
                state["next_turn_id"] = turn + 1
                if brief is not None:
                    state.setdefault("task_contexts", {})[request.task_id] = {"call_brief": brief}
                state["active_response_id"] = response_id
                state["messages"].append({"turn_id": turn, "text": request.text,
                                          "response_id": response_id, "task_id": request.task_id})
                state["responses"][response_id] = {
                    "response_id": response_id, "turn_id": turn, "status": "generating",
                    "task_id": request.task_id,
                    "generation": state["generation"], "input_revision": state["input_revision"],
                    "segments": [], "timeline": {}, "played_ms": None, "text_segment_ids": [],
                }
                pending.extend(self._invalidate_runs(state))
                result = {"session_id": sid, "turn_id": turn, "response_id": response_id}
                state["requests"][req_id] = {"digest": digest, "result": result}
                return (result, True), [*pending,
                    event("user.message", {"text": request.text}, author="user", public=True,
                          turn_id=turn, response_id=response_id),
                    event("response.started", {"mode": state["mode"]}, author="main", public=True,
                          turn_id=turn, response_id=response_id),
                ]
            result, is_new = await self._change(sid, apply)
            if not is_new:
                return result
            self._cancel_task(self.main_tasks.get(sid))
            self._cancel_invalid_work(sid, await self.repo.get(sid))
            self.trace_links[sid] = current_span_context()
            self.main_tasks[sid] = asyncio.create_task(self._launch(sid, result["response_id"]))
        return result

    async def _launch(self, sid, rid):
        state = await self.repo.get(sid)
        response = self._active(state, rid)
        await self.schedule(sid, "user.message", response.get("task_id", "default"))
        await self.respond(sid, rid)

    async def schedule(self, sid, trigger, task_id=None):
        async with self.locks[sid]:
            def apply(state, seq):
                if state["status"] != "open":
                    return [], []
                board = ensure_blackboard(state)
                scope = task_id or board["active_task_id"]
                if board["tasks"].get(scope, {}).get("status") != "active":
                    return [], []
                revision = state["input_revision"]
                scoped = [r for r in state["runs"].values()
                          if r.get("task_id", "default") == scope and self._fresh(state, r)]
                finished = {r["agent_id"] for r in scoped if r["status"] == "completed"}
                jobs, events = [], []
                for agent in state["agents"]:
                    reads = list(dict.fromkeys([*agent.get("reads", ["$message"]),
                        *(f"agent:{parent}" for parent in agent.get("depends_on", []))]))
                    versions = input_versions(state, scope, reads)
                    existing = any(r["agent_id"] == agent["agent_id"]
                                   and r.get("read_versions") == versions for r in scoped)
                    if (trigger not in agent["on"] or existing
                            or not set(agent["depends_on"]).issubset(finished)):
                        continue
                    run = {"run_id": str(uuid4()), "agent_id": agent["agent_id"],
                           "task_id": scope, "read_versions": versions,
                           "input_expires_at": self._input_expiry(state, scope, agent),
                           "input_revision": revision, "generation": state["generation"],
                           "turn_id": state["next_turn_id"] - 1, "status": "queued"}
                    state["runs"][run["run_id"]] = run
                    jobs.append((agent, run))
                    events.append(event("agent.queued", run, author=agent["agent_id"],
                                        turn_id=run["turn_id"]))
                return jobs, events
            jobs = await self._change(sid, apply)
            for agent, run in jobs:
                task = asyncio.create_task(self.run_background(sid, agent, run))
                self.background_tasks[sid][run["run_id"]] = task

    async def run_background(self, sid, agent, run):
        # фон переживает запрос: своя трасса (без родителя) с link на ход
        with trace.use_span(trace.INVALID_SPAN), span("agent.run", {
            "agent.name": agent["agent_id"], "session.id": sid, "turn.id": run["turn_id"],
            "call.generation": run["generation"], "run.id": run["run_id"],
        }, links=[c for c in [self.trace_links.get(sid)] if c]):
            await self._run_background(sid, agent, run)

    async def _run_background(self, sid, agent, run):
        run_id = run["run_id"]
        try:
            async with self.semaphores[sid], self.background_capacity, self.global_semaphore:
                async with asyncio.timeout(agent["timeout_seconds"]):
                    async with self.locks[sid]:
                        def start(state, seq):
                            if not self._fresh(state, run):
                                raise asyncio.CancelledError()
                            state["runs"][run_id]["status"] = "running"
                            return None, [event("agent.started", {"run_id": run_id},
                                                author=agent["agent_id"], turn_id=run["turn_id"])]
                        await self._change(sid, start)
                    state = await self.repo.get(sid)
                    async def tool(name, args):
                        if name not in agent["tools"]:
                            return {"error": "tool_not_allowed"}
                        return await self.tool(sid, run, agent["agent_id"], name, args)
                    result = await self.driver.run_background(
                        agent, package(state, agent=agent, task_id=run.get("task_id", "default")), tool,
                    )
                    async with self.locks[sid]:
                        def finish(state, seq):
                            accepted = self._fresh(state, run)
                            saved_run = state["runs"][run_id]
                            if accepted or saved_run["status"] != "cancelled":
                                saved_run["status"] = "completed" if accepted else "stale"
                            record = {**run, "summary": result.summary, "facts": result.facts,
                                      "source_ids": result.source_ids, "accepted": accepted,
                                      "status": saved_run["status"]}
                            pending = []
                            if accepted:
                                saved, pending = put_record(
                                    state, key=f"agent:{agent['agent_id']}", value=asdict(result),
                                    task_id=run.get("task_id", "default"), source="agent",
                                    source_id=run_id,
                                    expires_at=run.get("input_expires_at"),
                                    depends_on=[value for value in run.get("read_versions", {}).values()
                                                if value is not None],
                                )
                                record["record_id"] = saved["record_id"]
                                record["board_seq"] = seq + len(pending) + 1
                                state["background"].append(record)
                            return accepted, [*pending, event("agent.result" if accepted else "agent.stale",
                                record, author=agent["agent_id"], turn_id=run["turn_id"])]
                        accepted = await self._change(sid, finish)
                    if accepted:
                        await self.schedule(sid, "agent.result", run.get("task_id", "default"))
        except asyncio.CancelledError:
            await self._run_status(sid, run, "cancelled")
        except Exception as exc:  # noqa: BLE001 — isolate provider/task failures
            log.warning("Background run failed: %s", type(exc).__name__)
            await self._run_status(sid, run, "failed")

    @staticmethod
    def _fresh(state, run):
        if state["status"] != "open" or state["generation"] != run["generation"]:
            return False
        if run.get("input_expires_at") is not None and run["input_expires_at"] <= time.time():
            return False
        if "read_versions" not in run:
            # Foreground tools remain tied to this exact response, not a background fingerprint.
            return (state["input_revision"] == run["input_revision"]
                    and ("response_id" not in run or (
                        state["active_response_id"] == run["response_id"]
                        and state["responses"].get(run["response_id"], {}).get("status") == "generating"
                    )))
        saved = state["runs"].get(run.get("run_id"), run)
        return (saved["status"] not in ("stale", "cancelled")
                and fingerprint_matches(state, run.get("task_id", "default"), run["read_versions"]))

    async def _run_status(self, sid, run, status):
        async with self.locks[sid]:
            def apply(state, seq):
                saved = state["runs"].get(run["run_id"])
                if not saved or saved["status"] not in ("queued", "running"):
                    return None, []
                saved["status"] = status
                return None, [event("agent." + status, {"run_id": run["run_id"]},
                                    author=run["agent_id"], turn_id=run["turn_id"])]
            await self._change(sid, apply)

    async def respond(self, sid, rid):
        try:
            async with asyncio.timeout(settings.kernel_response_timeout):
                for index in range(settings.kernel_max_segments):
                    with span("segment", {"session.id": sid, "response.id": rid,
                                          "segment.index": index}) as seg:
                        result = await self.generate_segment(sid, rid, index)
                        seg.set_attribute("segment.used_source_ids", len(result.used_source_ids))
                    if not result.continue_response:
                        break
                async with self.locks[sid]:
                    def finish(state, seq):
                        response = self._active(state, rid)
                        response["status"] = "completed"
                        return None, [event("response.completed", {"finish_reason":
                            "segment_limit" if result.continue_response else "stop"},
                            author="main", public=True, response_id=rid, turn_id=response["turn_id"])]
                    await self._change(sid, finish)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — isolate provider/task failures
            log.warning("Foreground response failed: %s code=%s status=%s param=%s",
                        type(exc).__name__, str(exc) if isinstance(exc, ProviderError) else getattr(exc, "code", None),
                        getattr(exc, "status_code", None), getattr(exc, "param", None))
            async with self.locks[sid]:
                def fail(state, seq):
                    response = state["responses"].get(rid)
                    if not response or response["status"] != "generating":
                        return None, []
                    response["status"] = "failed"
                    for segment in response["segments"]:
                        if segment["status"] == "generating":
                            segment["status"] = "failed"
                    return None, [event("response.failed", {"code": "generation_failed",
                        "message": "Не удалось завершить ответ. Попробуйте новую реплику."},
                        author="main", public=True, response_id=rid, turn_id=response["turn_id"])]
                await self._change(sid, fail)

    async def generate_segment(self, sid, rid, index):
        state = await self.repo.get(sid)
        response = self._active(state, rid)
        ctx = package(state, rid, task_id=response.get("task_id", "default"))
        segment_id = str(uuid4())
        trace.get_current_span().set_attributes({"segment.id": segment_id, "turn.id": response["turn_id"]})
        async with self.locks[sid]:
            def start(state, seq):
                response = self._active(state, rid)
                response["segments"].append({"segment_id": segment_id, "index": index,
                    "text": "", "status": "generating", "used_source_ids": [],
                    "consumed_board_seq": ctx["consumed_board_seq"]})
                return None, [event("segment.started", {"index": index}, author="main",
                    public=True, response_id=rid, segment_id=segment_id,
                    turn_id=response["turn_id"])]
            await self._change(sid, start)
        async def emit(text):
            if not text:
                return
            async with self.locks[sid]:
                def append(state, seq):
                    response = self._active(state, rid)
                    segment = response["segments"][-1]
                    if len(segment["text"]) + len(text) > 6000:
                        raise KernelError("segment_limit", "Превышен размер сегмента")
                    segment["text"] += text
                    return None, [event("response.delta", {"text": text}, author="main",
                        public=True, response_id=rid, segment_id=segment_id,
                        turn_id=response["turn_id"])]
                await self._change(sid, append, durable=False)  # journal-only delta
        async def tool(name, args):
            return await self.tool(sid, response, "main", name, args)
        async with self.global_semaphore:
            result = await self.driver.stream_segment(ctx, emit, tool)
        async with self.locks[sid]:
            def complete(state, seq):
                response = self._active(state, rid)
                segment = response["segments"][-1]
                if segment["text"] != result.text:
                    raise ValueError("provider streaming text mismatch")
                segment["status"] = "completed"
                segment["used_source_ids"] = result.used_source_ids
                return None, [event("segment.completed", {
                    "used_source_ids": result.used_source_ids,
                    "consumed_board_seq": ctx["consumed_board_seq"],
                }, author="main", public=True, response_id=rid, segment_id=segment_id,
                    turn_id=response["turn_id"])]
            await self._change(sid, complete)
        return result

    @staticmethod
    def _active(state, rid):
        response = state["responses"].get(rid)
        if (state["status"] != "open" or state["active_response_id"] != rid
                or not response or response["status"] != "generating"):
            raise asyncio.CancelledError()
        return response

    async def tool(self, sid, run, author, name, args):
        try:
            if name == "blackboard_read":
                args = BlackboardReadArgs.model_validate(args).model_dump()
            elif name == "rag_search":
                args = RAGSearchArgs.model_validate(args).model_dump()
            elif name == "rag_read":
                args = RAGReadArgs.model_validate(args).model_dump()
            else:
                return {"error": "tool_not_allowed"}
        except ValueError:
            return {"error": "invalid_tool_arguments"}
        async with self.locks[sid]:
            def start(state, seq):
                if not self._fresh(state, run):
                    raise asyncio.CancelledError()
                return None, [event("tool.started", {"name": name, "arguments": args},
                                    author=author, turn_id=run["turn_id"])]
            await self._change(sid, start)
        span_name = {"rag_search": "rag.search", "rag_read": "kb.read",
                     "blackboard_read": "blackboard.read"}[name]
        with span(span_name) as tool_span:
            if name == "blackboard_read":
                state = await self.repo.get(sid)
                if not self._fresh(state, run):
                    raise asyncio.CancelledError()
                keys = args["keys"] or None
                if author != "main":
                    allowed = list(run.get("read_versions", {"$message": None}))
                    if "$message" not in allowed:
                        if keys and not set(keys).issubset(allowed):
                            return {"error": "undeclared_record_read"}
                        keys = allowed
                records = select_records(state, task_id=run.get("task_id", "default"), keys=keys)
                result = {"records": records}
            else:
                result = await self._shared_tool(sid, run, name, args)
            tool_span.set_attributes({k: v for k, v in tool_trace(
                sid, run["turn_id"], name, args, result).items() if v is not None})
        async with self.locks[sid]:
            def finish(state, seq):
                if self._fresh(state, run) and "error" not in result and name != "blackboard_read":
                    retrieval = {
                        "input_revision": run["input_revision"], "result": result,
                        "task_id": run.get("task_id", "default"), "generation": run["generation"],
                        "input_expires_at": run.get("input_expires_at"),
                    }
                    if "read_versions" in run:
                        retrieval["read_versions"] = run["read_versions"]
                    state.setdefault("retrieval", []).append(retrieval)
                    state["retrieval"] = state["retrieval"][-12:]
                return None, [event("tool.completed", {"name": name, "result": result,
                    "accepted": self._fresh(state, run)}, author=author, turn_id=run["turn_id"])]
            await self._change(sid, finish)
        return result

    async def _shared_tool(self, sid, run, name, args):
        fingerprint = ("background", run["read_versions"]) if "read_versions" in run else (
            "main", run["input_revision"]
        )
        key = (sid, run["generation"], run.get("task_id", "default"),
               json.dumps(fingerprint, sort_keys=True), name, json.dumps(args, sort_keys=True))
        if key not in self.tool_tasks:
            self.tool_tasks[key] = asyncio.create_task(self._execute_tool(name, args))
        self.tool_owners[key].append(run)
        return await asyncio.shield(self.tool_tasks[key])

    async def _execute_tool(self, name, args):
        try:
            async with asyncio.timeout(settings.llm_timeout_seconds):
                return await self.tool_executor(name, args)
        except Exception:  # noqa: BLE001 — expose only a stable tool error
            return {"error": "retrieval_unavailable"}

    @staticmethod
    async def _knowledge_tool(name, args):
        async with open_knowledge() as kb:
            if name == "rag_search":
                return await kb.search.rag_query(
                    args["query"], args["kinds"], args["limit"],
                    search_query=args.get("search_query"),
                )
            fact = await kb.read(args["kind"], args["key"])
            return fact.model_dump() if fact else {"error": "not_found"}

    async def playback(self, sid, request, *, interrupt=False):
        req_id, rid = str(request.request_id), str(request.response_id)
        digest = request_key("interrupt" if interrupt else "playback", request.model_dump(mode="json"))
        async with self.locks[sid]:
            def apply(state, seq):
                if prior := replay(state, req_id, digest):
                    return (prior["result"], False), []
                response = require_response(state, rid)
                if interrupt:
                    require_open(state)
                before = response.get("played_ms") or 0
                if not interrupt and response["status"] == "interrupted" and request.played_ms > before:
                    raise KernelError("playback_after_interrupt", "Нельзя продолжить прерванный ответ")
                if interrupt and response["status"] == "interrupted":
                    return ({"response_id": rid, "status": "interrupted", "played_ms": before}, False), []
                timeline = dict(response["timeline"])
                segments = {s["segment_id"]: s for s in response["segments"]}
                for timing in getattr(request, "segments", []):
                    seg_id = str(timing.segment_id)
                    if seg_id not in segments:
                        raise KernelError("segment_not_found", "Сегмент не принадлежит ответу", 422)
                    if segments[seg_id]["status"] == "generating":
                        raise KernelError("segment_incomplete", "Дождитесь окончания сегмента", 409)
                    interval = {"start_ms": timing.start_ms, "end_ms": timing.end_ms}
                    if seg_id in timeline and timeline[seg_id] != interval:
                        raise KernelError("timeline_conflict", "Границы сегмента уже зафиксированы")
                    timeline[seg_id] = interval
                end = 0
                for segment in response["segments"]:
                    if timing := timeline.get(segment["segment_id"]):
                        if timing["start_ms"] < end:
                            raise KernelError("timeline_overlap", "Интервалы должны идти по порядку", 422)
                        end = timing["end_ms"]
                delivered = [str(value) for value in getattr(request, "text_segment_ids", [])]
                if any(seg not in segments for seg in delivered):
                    raise KernelError("segment_not_found", "Сегмент не принадлежит ответу", 422)
                if any(segments[seg]["status"] == "generating" for seg in delivered):
                    raise KernelError("segment_incomplete", "Нельзя подтвердить растущий сегмент", 409)
                response["timeline"] = timeline
                response["text_segment_ids"] = list(set(response["text_segment_ids"]) | set(delivered))
                response["played_ms"] = max(before, request.played_ms)
                if interrupt:
                    response["status"] = "interrupted"
                    for segment in response["segments"]:
                        if segment["status"] == "generating":
                            segment["status"] = "interrupted"
                result = {"response_id": rid, "status": response["status"],
                          "played_ms": response["played_ms"]}
                state["requests"][req_id] = {"digest": digest, "result": result}
                return (result, interrupt and state["active_response_id"] == rid), [event(
                    "response.interrupted" if interrupt else "playback.updated", result,
                    author="user", public=interrupt, response_id=rid, turn_id=response["turn_id"])]
            result, cancel = await self._change(sid, apply)
            if cancel and (task := self.main_tasks.get(sid)):
                task.cancel()
            return result

    async def close_session(self, sid, reason="client_closed"):
        async with self.locks[sid]:
            def apply(state, seq):
                if state["status"] == "closed":
                    return {"session_id": sid, "status": "closed"}, []
                state["status"] = "closed"
                for response in state["responses"].values():
                    if response["status"] == "generating":
                        response["status"] = "interrupted"
                        for segment in response["segments"]:
                            if segment["status"] == "generating":
                                segment["status"] = "interrupted"
                for run in state["runs"].values():
                    if run["status"] in ("queued", "running"):
                        run["status"] = "cancelled"
                result = {"session_id": sid, "status": "closed"}
                return result, [event("session.closed", {"reason": reason}, public=True)]
            result = await self._change(sid, apply)
            if task := self.main_tasks.get(sid):
                task.cancel()
            for task in self.background_tasks[sid].values():
                task.cancel()
            for key, task in list(self.tool_tasks.items()):
                if key[0] == sid:
                    task.cancel()
                    self.tool_tasks.pop(key)
        return result

    async def shutdown(self):
        for sid in list(set(self.main_tasks) | set(self.background_tasks)):
            with suppress(KernelError):
                await self.close_session(sid, "server_shutdown")
        tasks = [*self.main_tasks.values(), *self.retired_tasks,
                 *(t for group in self.background_tasks.values() for t in group.values())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.driver.close()
