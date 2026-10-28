"""``simulate-research-query``: the server's task-based tool, and the ``tasks/*`` methods.

As recorded (000263-000308) and as the server's source and the SDK's in-memory task store do it:
the tool only runs as a task (a plain call is refused with ``-32601`` text); it answers at once
with a ``CreateTaskResult`` (ttl always 300000, pollInterval 1000) and then works through four
one-second stages, each announced with ``notifications/tasks/status`` on the client's GET
stream. An ambiguous topic, for a client that can elicit, goes ``input_required`` at stage three
and asks the client to clarify through the next ``tasks/result`` call (the request is queued
for it, tagged with the related task). ``tasks/result`` waits for the end and returns the report;
``tasks/cancel`` stops a task (no notification). Tasks vanish 300 s after creation, or 300 s
after they end.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from fauxcall.runtime import RpcError
from fauxcall.runtime.mcp_server import ClientError

from fauxcall_everything import jsvalues, sessions, zodlike

if TYPE_CHECKING:
    from collections.abc import Callable

    from fauxcall.runtime import RequestContext
    from fauxcall.runtime.mcp_server import Channel
    from fauxcall.types import JsonObject, JsonValue

TOOL: Final = "simulate-research-query"
STAGES: Final = (
    "Gathering sources",
    "Analyzing content",
    "Synthesizing findings",
    "Generating report",
)
STAGE_SECONDS: Final = 1.0
TTL_MS: Final = 300_000
POLL_INTERVAL_MS: Final = 1000
PAGE_SIZE: Final = 10
ELICITATION_TIMEOUT_S: Final = 60.0
TERMINAL: Final = frozenset({"completed", "failed", "cancelled"})
RELATED_TASK: Final = "io.modelcontextprotocol/related-task"
ARGS: Final = (zodlike.Arg("topic"), zodlike.Arg("ambiguous", "boolean", default=False))
_NS_PER_MS: Final = 1_000_000


def iso_ms(now_ns: int) -> str:
    """``new Date().toISOString()``: UTC, milliseconds, ``Z``."""
    moment = datetime.fromtimestamp(now_ns // _NS_PER_MS / 1000, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_ns // _NS_PER_MS % 1000:03d}Z"


@dataclass(slots=True)
class Task:
    """One research task of an MCP session."""

    task_id: str
    topic: str
    ambiguous: bool
    created_ns: int
    updated_ns: int
    status: str = "working"
    message: str | None = None
    clarification: JsonValue = None
    result: JsonObject | None = None
    ended_ns: int | None = None
    queue: list[tuple[JsonObject, asyncio.Future[JsonValue]]] = field(default_factory=list)
    """Requests queued for the client's next ``tasks/result`` (kept there even after they timed
    out, as the SDK's message queue keeps them: 000523)."""

    def as_json(self) -> JsonObject:
        """The task as ``tasks/get`` shows it."""
        shown: JsonObject = {
            "taskId": self.task_id,
            "status": self.status,
            "ttl": TTL_MS,
            "createdAt": iso_ms(self.created_ns),
            "lastUpdatedAt": iso_ms(self.updated_ns),
            "pollInterval": POLL_INTERVAL_MS,
        }
        if self.message is not None:
            shown["statusMessage"] = self.message
        return shown

    def expired(self, now_ns: int) -> bool:
        """Whether the store's cleanup timer has removed it."""
        start = self.ended_ns if self.ended_ns is not None else self.created_ns
        return now_ns >= start + TTL_MS * _NS_PER_MS


def _tasks(state: dict[str, Any]) -> dict[str, Task]:
    tasks: dict[str, Task] = state.setdefault("tasks", {})
    return tasks


def _clock(ctx: RequestContext) -> Callable[[], int]:
    clock = ctx.runtime.clocks.for_session(ctx.session)
    return clock.now_ns


def _find(ctx: RequestContext, task_id: str) -> Task | None:
    tasks = _tasks(sessions.of(ctx))
    task = tasks.get(task_id)
    if task is not None and task.expired(_clock(ctx)()):
        del tasks[task_id]
        return None
    return task


def _update(task: Task, status: str, message: str | None, now_ns: int, channel: Channel) -> bool:
    """``taskStore.updateTaskStatus``; ``False`` (nothing changes) once the task has ended."""
    if task.status in TERMINAL or task.expired(now_ns):  # the store refuses either
        return False
    task.status = status
    if message:
        task.message = message
    task.updated_ns = now_ns
    if status in TERMINAL:
        task.ended_ns = now_ns
    channel.push("notifications/tasks/status", task.as_json())
    return True


def report(task: Task) -> JsonObject:
    """The final result, word for word as the server writes it.

    A clarification that isn't a string (or a list of strings) breaks the server's
    ``state.clarification.includes(...)``: the task fails with that TypeError (000502-000503).
    """
    raw = task.clarification
    if jsvalues.truthy(raw) and not isinstance(raw, str | list):
        raise TypeError("state.clarification.includes is not a function")
    clarification = jsvalues.template(raw) if jsvalues.truthy(raw) else None
    unavailable = isinstance(raw, str | list) and "unavailable" in raw
    topic = f"{task.topic} ({clarification})" if clarification else task.topic
    stages = "\n".join(f"- Stage {i + 1}: {stage} ✓" for i, stage in enumerate(STAGES))
    progressed = "`input_required` → `working` → " if clarification else ""
    flow = ""
    if clarification:
        ending = (
            "**Note:** Elicitation failed and a default interpretation was used."
            if unavailable
            else f'After receiving clarification ("{clarification}"), the task resumed '
            "processing and completed."
        )
        flow = (
            "**Elicitation Flow:**\n"
            "When the query was ambiguous, the server sent an `elicitation/create` request\n"
            "to the client. The task status changed to `input_required` while awaiting user "
            f"input.\n{ending}\n"
        )
    text = (
        f"# Research Report: {topic}\n\n"
        "## Research Parameters\n"
        f"- **Topic**: {task.topic}\n"
        f"{f'- **Clarification**: {clarification}' if clarification else ''}\n\n"
        "## Synthesis\n"
        f"This research query was processed through {len(STAGES)} stages:\n"
        f"{stages}\n\n"
        "---\n\n"
        "## About This Demo (SEP-1686: Tasks)\n\n"
        "This tool demonstrates MCP's task-based execution pattern for long-running operations:\n\n"
        "**Task Lifecycle Demonstrated:**\n"
        "1. `tools/call` with `task` parameter → Server returns `CreateTaskResult` (not the "
        "final result)\n"
        "2. Client polls `tasks/get` → Server returns current status and `statusMessage`\n"
        f"3. Status progressed: `working` → {progressed}`completed`\n"
        "4. Client calls `tasks/result` → Server returns this final result\n\n"
        f"{flow}\n"
        "**Key Concepts:**\n"
        '- Tasks enable "call now, fetch later" patterns\n'
        "- `statusMessage` provides human-readable progress updates\n"
        "- Tasks have TTL (time-to-live) for automatic cleanup\n"
        "- `pollInterval` suggests how often to check status\n"
        "- Elicitation requests use `relatedTask` to queue via tasks/result (works on all "
        "transports)\n\n"
        "*This is a simulated research report from the Everything MCP Server.*\n"
    )
    return {"content": [{"type": "text", "text": text}]}


def interpretations(topic: str) -> list[JsonValue]:
    """The choices the clarification request offers for a topic."""
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


def _clarification(answer: JsonValue) -> JsonValue:
    """What the task continues with, from the client's answer to the clarification request.

    ``interpretation || "User accepted without selection"``: a number or a list is kept as such.
    """
    parsed, problems = sessions.elicit_result(answer)
    if problems:
        return "technical (default - elicitation unavailable)"
    content = parsed.get("content")
    if parsed["action"] == "accept" and jsvalues.truthy(content):
        chosen = content.get("interpretation") if isinstance(content, dict) else None
        return chosen if jsvalues.truthy(chosen) else "User accepted without selection"
    if parsed["action"] == "decline":
        return "User declined - using default interpretation"
    return "User cancelled - using default interpretation"


async def _ask(task: Task, channel: Channel) -> JsonValue:
    """Queue the clarification request for ``tasks/result`` and wait for the answer (60 s)."""
    params: JsonObject = {
        "message": f'The research query "{task.topic}" could have multiple interpretations. '
        "Please clarify what you're looking for:",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "interpretation": {
                    "type": "string",
                    "title": "Clarification",
                    "description": "Which interpretation of the topic do you mean?",
                    "oneOf": interpretations(task.topic),
                }
            },
            "required": ["interpretation"],
        },
        "_meta": {RELATED_TASK: {"taskId": task.task_id}},
    }
    answered: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
    task.queue.append((params, answered))
    timeout = asyncio.ensure_future(channel.pause(ELICITATION_TIMEOUT_S))
    try:
        racing: set[asyncio.Future[Any]] = {answered, timeout}
        await asyncio.wait(racing, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timeout.cancel()
    if not answered.done() or answered.exception() is not None:
        return "technical (default - elicitation unavailable)"
    return _clarification(answered.result())


def invalid_task_result() -> RpcError:
    """A task request whose tool answered no ``CreateTaskResult`` (the SDK checks it)."""
    issue = zodlike.type_issue(["task"], "object", None, present=False)
    return RpcError(
        zodlike.INVALID_PARAMS,
        "MCP error -32602: Invalid task creation result: "
        + json.dumps([issue], indent=2, ensure_ascii=False),
    )


async def call(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tools/call`` of the research tool: a task, or the real refusals."""
    if "task" not in params:
        return zodlike.tool_error(
            f"MCP error -32601: Tool {TOOL} requires task augmentation (taskSupport: 'required')"
        )
    arguments = params.get("arguments")
    parsed, issues = zodlike.check(ARGS, arguments if isinstance(arguments, dict) else {})
    if issues:  # the tool's error result is no CreateTaskResult: the SDK refuses it
        raise invalid_task_result()
    clock = _clock(ctx)
    now = clock()
    task = Task(
        task_id=f"{ctx.rng.getrandbits(128):032x}",
        topic=str(parsed["topic"]),
        ambiguous=bool(parsed["ambiguous"]) and "elicitation" in ctx.client_capabilities,
        created_ns=now,
        updated_ns=now,
    )
    _tasks(sessions.of(ctx))[task.task_id] = task
    channel = ctx.channel
    if channel is None:  # pragma: no cover - MCP requests always have one
        raise RpcError(zodlike.INTERNAL_ERROR, "No task store provided.")
    _update(task, "working", f"{STAGES[0]}...", now, channel)

    async def work(stream: Channel) -> None:
        await stream.pause(STAGE_SECONDS)
        with contextlib.suppress(asyncio.CancelledError):
            await _run_from_second_stage(task, clock, stream)

    ctx.background(work)
    return {"task": task.as_json()}


async def _run_from_second_stage(task: Task, clock: Callable[[], int], channel: Channel) -> None:
    """The process after the first stage, which the call itself announced."""
    for index, stage in enumerate(STAGES[1:], start=1):
        if not _update(task, "working", f"{stage}...", clock(), channel):
            return
        if index == 2 and task.ambiguous and task.clarification is None:  # noqa: PLR2004
            waiting = (
                f'Found multiple interpretations for "{task.topic}". Requesting clarification...'
            )
            _update(task, "input_required", waiting, clock(), channel)
            _update(task, "input_required", waiting, clock(), channel)  # the queued request
            task.clarification = await _ask(task, channel)
            shown = jsvalues.template(task.clarification)
            if not _update(
                task, "working", f'Continuing with interpretation: "{shown}"...', clock(), channel
            ):
                return
        await channel.pause(STAGE_SECONDS)
    if task.status in TERMINAL:
        return
    try:
        task.result = report(task)
    except TypeError as error:  # the process's catch: the task fails with String(error)
        _update(task, "failed", f"TypeError: {error}", clock(), channel)
        return
    _update(task, "completed", None, clock(), channel)


def _task_id(params: JsonObject) -> str:
    return zodlike.require_string(params, "taskId")


async def get(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/get``."""
    task = _find(ctx, _task_id(params))
    if task is None:
        raise RpcError(
            zodlike.INVALID_PARAMS, "MCP error -32602: Failed to retrieve task: Task not found"
        )
    return task.as_json()


async def list_tasks(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/list``: ten a page, the cursor being the last task id of the previous page."""
    now = _clock(ctx)()
    tasks = _tasks(sessions.of(ctx))
    for task_id in [key for key, task in tasks.items() if task.expired(now)]:
        del tasks[task_id]
    ids = list(tasks)
    start = 0
    cursor = params.get("cursor")
    if "cursor" in params and not isinstance(cursor, str):
        raise zodlike.request_error(
            [zodlike.type_issue(["params", "cursor"], "string", cursor, present=True)]
        )
    if cursor:
        if cursor not in ids:
            raise RpcError(
                zodlike.INVALID_PARAMS,
                "MCP error -32602: Failed to list tasks: Invalid cursor: "
                + jsvalues.template(cursor),
            )
        start = ids.index(str(cursor)) + 1
    page = ids[start : start + PAGE_SIZE]
    listed: JsonObject = {"tasks": [tasks[task_id].as_json() for task_id in page]}
    if start + PAGE_SIZE < len(ids):
        listed["nextCursor"] = page[-1]
    listed["_meta"] = {}
    return listed


async def cancel(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/cancel``."""
    task_id = _task_id(params)
    task = _find(ctx, task_id)
    if task is None:
        raise RpcError(zodlike.INVALID_PARAMS, f"MCP error -32602: Task not found: {task_id}")
    if task.status in TERMINAL:
        raise RpcError(
            zodlike.INVALID_PARAMS,
            f"MCP error -32602: Cannot cancel task in terminal status: {task.status}",
        )
    now = _clock(ctx)()
    task.status = "cancelled"
    task.message = "Client cancelled task execution."
    task.updated_ns = now
    task.ended_ns = now
    task.queue.clear()  # the SDK clears the task's message queue
    return {"_meta": {}, **task.as_json()}


_DELIVERING: set[asyncio.Task[None]] = set()


def _deliver(ctx: RequestContext, request: JsonObject, answered: asyncio.Future[JsonValue]) -> None:
    """Send a queued request on this ``tasks/result`` stream; its answer reaches the task."""

    async def ask() -> None:
        try:
            answer = await ctx.ask_client("elicitation/create", request)
        except ClientError as error:
            if not answered.done():
                answered.set_exception(error)
        else:
            if not answered.done():
                answered.set_result(answer)

    sending = asyncio.ensure_future(ask())
    _DELIVERING.add(sending)
    sending.add_done_callback(_DELIVERING.discard)


async def result(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/result``: answers on the ``pollInterval`` grid of the call.

    Each tick (from the call on, one a second) delivers what the task queued for the client,
    then answers if the task has ended (000299: the result 1 s after the task completed;
    000507: 3009 ms). A cancelled call stops waiting and leaves the queue for the next one.
    """
    task_id = _task_id(params)
    task = _find(ctx, task_id)
    if task is None:
        raise RpcError(zodlike.INVALID_PARAMS, f"MCP error -32602: Task not found: {task_id}")
    channel = ctx.channel
    while True:
        if ctx.cancelled:
            raise RpcError(zodlike.INTERNAL_ERROR, "Request was cancelled")
        while task.queue:
            request, answered = task.queue.pop(0)
            _deliver(ctx, request, answered)
            await asyncio.sleep(0)  # sent before anything else goes out on this stream
        if task.status == "completed" and task.result is not None:
            return {**task.result, "_meta": {RELATED_TASK: {"taskId": task.task_id}}}
        if task.status in TERMINAL:
            raise RpcError(zodlike.INTERNAL_ERROR, f"Task {task.task_id} has no result stored")
        if channel is None:  # pragma: no cover - MCP requests always have one
            raise RpcError(zodlike.INTERNAL_ERROR, "No channel")
        await channel.pause(POLL_INTERVAL_MS / 1000)
