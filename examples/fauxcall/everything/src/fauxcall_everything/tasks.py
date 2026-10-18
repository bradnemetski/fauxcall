"""MCP tasks: the SDK's in-memory task store, ``simulate-research-query`` and ``tasks/*``.

Each client's server has its own store. A research task runs in the background on the wall
clock, like the real one (``Gathering sources`` -> ``Analyzing content`` -> ``Synthesizing
findings`` -> ``Generating report``, one second each), and every status change is pushed to the
client's GET stream as ``notifications/tasks/status``. An ambiguous topic from a client that can
elicit asks for a clarification: the request waits in the task's queue until the client calls
``tasks/result``, which delivers it on its own stream, as the SDK's task message queue does.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from fauxcall.runtime import ClientError, RpcError

from fauxcall_everything import js, requests, zod
from fauxcall_everything.state import server_of, session_of
from fauxcall_everything.validation import elicit_result

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.types import JsonObject, JsonValue

    from fauxcall_everything.state import McpSession, Server

TERMINAL: Final = frozenset({"completed", "failed", "cancelled"})
RELATED_TASK: Final = "io.modelcontextprotocol/related-task"
RESEARCH_TTL_MS: Final = 300_000
POLL_INTERVAL_MS: Final = 1000
LIST_PAGE_SIZE: Final = 10
REQUEST_TIMEOUT_S: Final = 60.0
"""The SDK's default request timeout: a clarification nobody fetches gives up after a minute."""
STAGES: Final = (
    "Gathering sources",
    "Analyzing content",
    "Synthesizing findings",
    "Generating report",
)
STAGE_SECONDS: Final = 1.0
TICK: float = 1.0
"""Seconds per second of the real server's task timers (stages, polling): tests shrink it."""
DEFAULT_CLARIFICATION: Final = "technical (default - elicitation unavailable)"
_DELIVERIES: Final[set[asyncio.Future[None]]] = set()
"""Queued requests on their way to the client (kept so they aren't garbage-collected)."""


class TaskError(Exception):
    """The store refused a status change (gone, or already terminal)."""


class ClarificationTypeError(TypeError):
    """``state.clarification.includes is not a function``: a clarification that isn't text."""


@dataclass(slots=True)
class QueuedRequest:
    """A request to the client waiting for ``tasks/result`` to deliver it."""

    method: str
    params: JsonObject
    answer: asyncio.Future[JsonValue]


@dataclass(slots=True)
class Task:
    """One task, as the store keeps it."""

    task_id: str
    ttl: int | None
    created_at: str
    last_updated_at: str
    status: str = "working"
    poll_interval: int = POLL_INTERVAL_MS
    status_message: str | None = None
    result: JsonObject | None = None
    expires_at: float | None = None
    queue: list[QueuedRequest] = field(default_factory=list)

    def as_json(self) -> JsonObject:
        """The task object (members in the order the store creates them)."""
        body: JsonObject = {
            "taskId": self.task_id,
            "status": self.status,
            "ttl": self.ttl,
            "createdAt": self.created_at,
            "lastUpdatedAt": self.last_updated_at,
            "pollInterval": self.poll_interval,
        }
        if self.status_message is not None:
            body["statusMessage"] = self.status_message
        return body


@dataclass(slots=True)
class TaskStore:
    """``InMemoryTaskStore`` for one client: tasks expire ``ttl`` after creation or completion."""

    server: Server
    session: McpSession
    tasks: dict[str, Task] = field(default_factory=dict)

    def create(self, ttl: int | None) -> Task:
        """A new working task."""
        now = js.iso_ms(
            self.server.runtime.clocks.for_session(self.server.fauxcall_session).now_ns()
        )
        task = Task(secrets.token_hex(16), ttl, now, now)
        if ttl:
            task.expires_at = time.monotonic() + ttl / 1000
        self.tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        """A task that still exists."""
        task = self.tasks.get(task_id)
        if task is not None and task.expires_at is not None and time.monotonic() >= task.expires_at:
            del self.tasks[task_id]
            return None
        return task

    def visible(self) -> list[Task]:
        """Every task that still exists, oldest first."""
        return [task for task_id in list(self.tasks) if (task := self.get(task_id)) is not None]

    def _touch(self, task: Task) -> None:
        clock = self.server.runtime.clocks.for_session(self.server.fauxcall_session)
        task.last_updated_at = js.iso_ms(clock.now_ns())
        if task.status in TERMINAL and task.ttl:
            task.expires_at = time.monotonic() + task.ttl / 1000

    def set_status(self, task: Task, status: str, message: str | None = None) -> None:
        """``updateTaskStatus`` without a notification (``tasks/cancel`` does that)."""
        if task.status in TERMINAL:
            raise TaskError(task.task_id)
        task.status = status
        if message:
            task.status_message = message
        self._touch(task)

    def update(self, channel: Channel, task: Task, status: str, message: str | None = None) -> None:
        """The request task store's ``updateTaskStatus``: the change, then its notification."""
        if self.get(task.task_id) is None:
            raise TaskError(task.task_id)
        self.set_status(task, status, message)
        channel.push("notifications/tasks/status", task.as_json())

    def complete(self, channel: Channel, task: Task, result: JsonObject) -> None:
        """``storeTaskResult``: the result, the terminal status, and the notification."""
        if self.get(task.task_id) is None or task.status in TERMINAL:
            raise TaskError(task.task_id)
        task.result = result
        task.status = "completed"
        self._touch(task)
        channel.push("notifications/tasks/status", task.as_json())

    async def ask(self, task: Task, method: str, params: JsonObject) -> JsonValue:
        """Queue a request for the client (``relatedTask``) and wait for its answer."""
        answer: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        related: JsonObject = {**params, "_meta": {RELATED_TASK: {"taskId": task.task_id}}}
        task.queue.append(QueuedRequest(method, related, answer))
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S * TICK):
                return await asyncio.shield(answer)
        except TimeoutError as error:
            raise ClientError(-32001, "Request timed out") from error


def store_of(ctx: RequestContext) -> TaskStore:
    """The calling client's task store."""
    session = session_of(ctx)
    if session.tasks is None:
        session.tasks = TaskStore(server_of(ctx), session)
    return session.tasks


# -- simulate-research-query -----------------------------------------------------------------------


RESEARCH_SCHEMA: Final = zod.ToolSchema(
    "simulate-research-query",
    {
        "topic": zod.Field(zod.string()),
        "ambiguous": zod.Field(zod.boolean(), default=False),
    },
)


@dataclass(slots=True)
class Research:
    """``researchStates``: one research task's progress."""

    topic: str
    ambiguous: bool
    stage: int = 0
    completed: bool = False
    clarification: JsonValue = None


def interpretations(topic: str) -> list[JsonValue]:
    """``getInterpretationsForTopic``."""
    if "python" in topic.lower():
        return [
            {"const": "programming", "title": "Python programming language"},
            {"const": "snake", "title": "Python snake species"},
            {"const": "comedy", "title": "Monty Python comedy group"},
        ]
    return [
        {"const": "technical", "title": "Technical/scientific perspective"},
        {"const": "historical", "title": "Historical perspective"},
        {"const": "current", "title": "Current events/news perspective"},
    ]


def report(state: Research) -> JsonObject:
    """``generateResearchReport`` (raises ``TypeError`` where the real one does)."""
    clarification = state.clarification
    clarified = js.js_truthy(clarification)
    if clarified and not isinstance(clarification, str | list):
        raise ClarificationTypeError("state.clarification.includes is not a function")
    shown = js.js_string(clarification) if clarified else ""
    topic = f"{state.topic} ({shown})" if clarified else state.topic
    stages = "\n".join(f"- Stage {i + 1}: {stage} ✓" for i, stage in enumerate(STAGES))
    progressed = "`input_required` → `working` → " if clarified else ""
    flow = ""
    if clarified:
        unavailable = (
            "unavailable" in clarification if isinstance(clarification, str | list) else False
        )
        ending = (
            "**Note:** Elicitation failed and a default interpretation was used."
            if unavailable
            else f'After receiving clarification ("{shown}"), the task resumed processing and '
            "completed."
        )
        flow = (
            "**Elicitation Flow:**\n"
            "When the query was ambiguous, the server sent an `elicitation/create` request\n"
            "to the client. The task status changed to `input_required` while awaiting user "
            "input.\n"
            f"{ending}\n"
        )
    text = (
        f"# Research Report: {topic}\n\n"
        "## Research Parameters\n"
        f"- **Topic**: {state.topic}\n"
        f"{f'- **Clarification**: {shown}' if clarified else ''}\n\n"
        "## Synthesis\n"
        f"This research query was processed through {len(STAGES)} stages:\n"
        f"{stages}\n\n"
        "---\n\n"
        "## About This Demo (SEP-1686: Tasks)\n\n"
        "This tool demonstrates MCP's task-based execution pattern for long-running operations:\n\n"
        "**Task Lifecycle Demonstrated:**\n"
        "1. `tools/call` with `task` parameter → Server returns `CreateTaskResult` "
        "(not the final result)\n"
        "2. Client polls `tasks/get` → Server returns current status and `statusMessage`\n"
        f"3. Status progressed: `working` → {progressed}`completed`\n"
        "4. Client calls `tasks/result` → Server returns this final result\n\n"
        f"{flow}\n"
        "**Key Concepts:**\n"
        '- Tasks enable "call now, fetch later" patterns\n'
        "- `statusMessage` provides human-readable progress updates\n"
        "- Tasks have TTL (time-to-live) for automatic cleanup\n"
        "- `pollInterval` suggests how often to check status\n"
        "- Elicitation requests use `relatedTask` to queue via tasks/result "
        "(works on all transports)\n\n"
        "*This is a simulated research report from the Everything MCP Server.*\n"
    )
    return {"content": [{"type": "text", "text": text}]}


def start_research(ctx: RequestContext, arguments: JsonObject) -> JsonObject:
    """``createTask``: the task (already ``Gathering sources...``), its research started."""
    parsed, refusal = RESEARCH_SCHEMA.parse(arguments)
    if refusal is not None:  # a tool error is no CreateTaskResult
        raise RpcError(-32602, invalid_task_result())
    store = store_of(ctx)
    task = store.create(RESEARCH_TTL_MS)
    supports_elicitation = "elicitation" in ctx.client_capabilities
    state = Research(str(parsed["topic"]), bool(parsed["ambiguous"]) and supports_elicitation)
    assert ctx.channel is not None  # noqa: S101 - every MCP call has one
    store.update(ctx.channel, task, "working", f"{STAGES[0]}...")

    async def research(channel: Channel) -> None:
        with contextlib.suppress(TaskError):  # cancelled meanwhile: the real one fails silently
            await _research(store, task, state, channel)

    ctx.background(research)
    return {"task": task.as_json()}


async def _research(store: TaskStore, task: Task, state: Research, channel: Channel) -> None:
    """``runResearchProcess`` after its first status update."""
    try:
        for index in range(len(STAGES)):
            state.stage = index
            if state.completed or not store.server.alive():
                return
            if index > 0:
                store.update(channel, task, "working", f"{STAGES[index]}...")
            if index == 2 and state.ambiguous and not js.js_truthy(state.clarification):  # noqa: PLR2004 - the synthesis stage
                await _clarify(store, task, state, channel)
            await channel.pause(STAGE_SECONDS * TICK)
        state.completed = True
        result = report(state)
    except ClarificationTypeError as error:  # the real report breaks on non-text clarifications
        with contextlib.suppress(TaskError):
            store.update(channel, task, "failed", f"TypeError: {error}")
        return
    store.complete(channel, task, result)


async def _clarify(store: TaskStore, task: Task, state: Research, channel: Channel) -> None:
    """Ask the client which interpretation it means (``input_required`` meanwhile)."""
    store.update(
        channel,
        task,
        "input_required",
        f'Found multiple interpretations for "{state.topic}". Requesting clarification...',
    )
    try:
        store.update(channel, task, "input_required")  # sendRequest marks the task again
        params: JsonObject = {
            "message": f'The research query "{state.topic}" could have multiple interpretations. '
            "Please clarify what you're looking for:",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "interpretation": {
                        "type": "string",
                        "title": "Clarification",
                        "description": "Which interpretation of the topic do you mean?",
                        "oneOf": interpretations(state.topic),
                    }
                },
                "required": ["interpretation"],
            },
        }
        answer = await store.ask(task, "elicitation/create", params)
        result = elicit_result(answer)
        content = result.get("content")
        if result.get("action") == "accept" and js.js_truthy(content):
            chosen = content.get("interpretation") if isinstance(content, dict) else None
            state.clarification = (
                chosen if js.js_truthy(chosen) else "User accepted without selection"
            )
        elif result.get("action") == "decline":
            state.clarification = "User declined - using default interpretation"
        else:
            state.clarification = "User cancelled - using default interpretation"
    except (ClientError, RpcError, TaskError, ValueError):
        state.clarification = DEFAULT_CLARIFICATION
    store.update(
        channel,
        task,
        "working",
        f'Continuing with interpretation: "{js.js_string(state.clarification)}"...',
    )


def invalid_task_result() -> str:
    """The refusal of a task-augmented call whose tool answered without a task."""
    issue = zod.wrong_type("object", zod.MISSING, ["task"])
    return f"MCP error -32602: Invalid task creation result: {zod.issues_text([issue])}"


# -- tasks/* ---------------------------------------------------------------------------------------


def _task_id(ctx: RequestContext, params: JsonObject) -> str:
    missing = requests.params_issue(ctx)
    requests.refuse([missing] if missing is not None else [])
    task_id = params.get("taskId", zod.MISSING)
    if not isinstance(task_id, str):
        raise RpcError(
            -32603, zod.issues_text([zod.wrong_type("string", task_id, ["params", "taskId"])])
        )
    return task_id


async def tasks_get(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/get``: the task, or ``Failed to retrieve task: Task not found``."""
    task = store_of(ctx).get(_task_id(ctx, params))
    if task is None:
        raise RpcError(-32602, "MCP error -32602: Failed to retrieve task: Task not found")
    return task.as_json()


async def tasks_list(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/list``: ten tasks a page, the cursor being the last task id of a page."""
    cursor = params.get("cursor", zod.MISSING)
    if cursor is not zod.MISSING and not isinstance(cursor, str):
        raise RpcError(
            -32603, zod.issues_text([zod.wrong_type("string", cursor, ["params", "cursor"])])
        )
    tasks = store_of(ctx).visible()
    start = 0
    if isinstance(cursor, str) and cursor:
        ids = [task.task_id for task in tasks]
        if cursor not in ids:
            raise RpcError(
                -32602, f"MCP error -32602: Failed to list tasks: Invalid cursor: {cursor}"
            )
        start = ids.index(cursor) + 1
    page = tasks[start : start + LIST_PAGE_SIZE]
    body: JsonObject = {"tasks": [task.as_json() for task in page]}
    if start + LIST_PAGE_SIZE < len(tasks):
        body["nextCursor"] = page[-1].task_id
    body["_meta"] = {}
    return body


async def tasks_cancel(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/cancel``: the cancelled task; terminal tasks can't be cancelled."""
    task_id = _task_id(ctx, params)
    store = store_of(ctx)
    task = store.get(task_id)
    if task is None:
        raise RpcError(-32602, f"MCP error -32602: Task not found: {task_id}")
    if task.status in TERMINAL:
        raise RpcError(
            -32602, f"MCP error -32602: Cannot cancel task in terminal status: {task.status}"
        )
    store.set_status(task, "cancelled", "Client cancelled task execution.")
    task.queue.clear()
    return {"_meta": {}, **task.as_json()}


async def tasks_result(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/result``: deliver what the task queued for the client, block until it ends."""
    task_id = _task_id(ctx, params)
    store = store_of(ctx)
    while True:
        task = store.get(task_id)
        if task is None:
            raise RpcError(-32602, f"MCP error -32602: Task not found: {task_id}")
        while task.queue:
            queued = task.queue.pop(0)
            delivery = asyncio.ensure_future(
                _deliver(ctx.ask_client(queued.method, queued.params), queued)
            )
            _DELIVERIES.add(delivery)
            delivery.add_done_callback(_DELIVERIES.discard)
        task = store.get(task_id)
        if task is None:
            raise RpcError(-32602, f"MCP error -32602: Task not found: {task_id}")
        if task.status in TERMINAL:
            break
        await asyncio.sleep(task.poll_interval / 1000 * TICK)  # the SDK polls, it doesn't wait
    if task.result is None:
        raise RpcError(-32603, f"Task {task_id} has no result stored")
    task.queue.clear()
    result: JsonObject = dict(task.result)
    meta = result.get("_meta")
    result["_meta"] = {
        **(meta if isinstance(meta, dict) else {}),
        RELATED_TASK: {"taskId": task_id},
    }
    return result


async def _deliver(asking: Awaitable[JsonValue], queued: QueuedRequest) -> None:
    """Hand the client's answer (or refusal) to the request waiting for it."""
    try:
        answer = await asking
    except ClientError as error:
        if not queued.answer.done():
            queued.answer.set_exception(error)
        return
    if not queued.answer.done():
        queued.answer.set_result(answer)
