"""``simulate-research-query`` and its tasks (``tasks/get``, ``result``, ``list``, ``cancel``).

As on the real server (the SDK's ``InMemoryTaskStore`` per session): a task runs four one-second
stages, sending ``notifications/tasks/status`` on each change; an ambiguous topic from a client
that can elicit stops at the third stage (``input_required``) until the client answers the
elicitation, which is delivered on its next ``tasks/result``; ``tasks/result`` checks once per poll
interval (1 s) and answers when the task is done. Tasks expire 5 minutes after they were created
or finished.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from fauxcall.runtime import ClientError, RpcError

from fauxcall_everything import jsfmt, zod
from fauxcall_everything.common import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    NS_PER_MS,
    NS_PER_S,
    ClientState,
    check_cursor,
    client_state,
    iso_ms,
    mcp_error_text,
    now_ns,
    protocol_error,
    task_creation_error,
    text,
    tool_error,
    wants_task,
)

if TYPE_CHECKING:
    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.runtime.app import FakeRuntime
    from fauxcall.types import JsonObject, JsonValue

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
TERMINAL: Final = frozenset({"completed", "failed", "cancelled"})
RELATED_TASK: Final = "io.modelcontextprotocol/related-task"
TOOL: Final = "simulate-research-query"
REQUEST_TIMEOUT_SECONDS: Final = 60.0
REQUEST_TIMEOUT: Final = -32001
CLARIFY_AT: Final = 2
RELAY_SECONDS: Final = 3600.0
"""A delivered clarification waits this long at most; the research times it out first."""
"""The stage at which an ambiguous topic asks the client for clarification."""
FIELDS: Final = [zod.Field("topic", "string"), zod.Field("ambiguous", "boolean", default=False)]


@dataclass(slots=True)
class ResearchTask:
    """One task and the research behind it."""

    task_id: str
    created_ns: int
    topic: str
    ambiguous: bool
    status: str = "working"
    status_message: str | None = None
    updated_ns: int = 0
    expires_ns: int = 0
    clarification: str | None = None
    result: JsonObject | None = None
    elicitation: JsonObject | None = None
    """The elicitation request waiting for the client's next ``tasks/result``."""
    answer: asyncio.Future[JsonValue] | None = None
    """Where the client's answer to the clarification lands (the research waits on it)."""
    relay: asyncio.Task[None] | None = None
    """The delivered request, waiting for the client's answer (in the background)."""
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def snapshot(self) -> JsonObject:
        """The task as ``tasks/get`` answers it."""
        task: JsonObject = {
            "taskId": self.task_id,
            "status": self.status,
            "ttl": TTL_MS,
            "createdAt": iso_ms(self.created_ns),
            "lastUpdatedAt": iso_ms(self.updated_ns),
            "pollInterval": POLL_INTERVAL_MS,
        }
        if self.status_message is not None:
            task["statusMessage"] = self.status_message
        return task


class _TerminalError(Exception):
    """``updateTaskStatus`` on a finished task (the research stops)."""


def _tasks(state: ClientState) -> dict[str, ResearchTask]:
    return state.tasks


def _live(
    runtime: FakeRuntime, session: str, state: ClientState, task_id: object
) -> ResearchTask | None:
    """A task of this client that hasn't expired."""
    task = _tasks(state).get(str(task_id))
    if task is None:
        return None
    if now_ns(runtime, session) >= task.expires_ns:
        del _tasks(state)[task.task_id]
        return None
    return task


@dataclass(frozen=True, slots=True)
class _Runner:
    """Where a task's research runs: the fake's runtime, its evaluation session, the client."""

    runtime: FakeRuntime
    session: str
    channel: Channel

    def now(self) -> int:
        """The session's clock."""
        return now_ns(self.runtime, self.session)

    async def wait(self, seconds: float) -> None:
        """Wait on the session's clock (advancing it ends the wait)."""
        await self.runtime.wait(self.session, seconds)

    async def until(self, deadline_ns: int) -> None:
        """Wait until the session's clock reaches ``deadline_ns`` (at once if it already has).

        Stages are scheduled from the task's start, so a clock moved far ahead finishes every
        stage that is due, as that much time would have on the real server.
        """
        left_ns = deadline_ns - self.now()
        if left_ns > 0:
            await self.wait(left_ns / NS_PER_S)

    def update(
        self, task: ResearchTask, status: str, message: str | None, at_ns: int | None = None
    ) -> None:
        """``updateTaskStatus``, and the status notification the SDK sends for it."""
        if task.status in TERMINAL:
            raise _TerminalError(task.task_id)
        task.status = status
        if message:
            task.status_message = message
        task.updated_ns = min(at_ns, self.now()) if at_ns is not None else self.now()
        if status in TERMINAL:
            task.expires_ns = task.updated_ns + TTL_MS * NS_PER_MS
        self.channel.push("notifications/tasks/status", task.snapshot())
        task.changed.set()


def _interpretations(topic: str) -> list[JsonValue]:
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


def _clarification(answer: JsonValue | BaseException) -> str:
    if isinstance(answer, BaseException) or not isinstance(answer, dict):
        return "technical (default - elicitation unavailable)"
    action = answer.get("action")
    content = answer.get("content")
    if action not in {"accept", "decline", "cancel"} or (
        content is not None and not isinstance(content, dict)
    ):
        return "technical (default - elicitation unavailable)"  # the SDK refused the answer
    if action == "accept" and isinstance(content, dict):
        interpretation = content.get("interpretation")
        return (
            jsfmt.js_string(interpretation)
            if interpretation not in (None, "", 0, False)
            else "User accepted without selection"
        )
    if action == "decline":
        return "User declined - using default interpretation"
    return "User cancelled - using default interpretation"


def report(task: ResearchTask) -> JsonObject:
    """``generateResearchReport``."""
    clarified = task.clarification
    heading = f"{task.topic} ({clarified})" if clarified else task.topic
    stages = "\n".join(f"- Stage {index + 1}: {stage} ✓" for index, stage in enumerate(STAGES))
    progression = "`input_required` → `working` → " if clarified else ""
    flow = ""
    if clarified:
        outcome = (
            "**Note:** Elicitation failed and a default interpretation was used."
            if "unavailable" in clarified
            else f'After receiving clarification ("{clarified}"), the task resumed processing '
            "and completed."
        )
        flow = (
            "**Elicitation Flow:**\n"
            "When the query was ambiguous, the server sent an `elicitation/create` request\n"
            "to the client. The task status changed to `input_required` while awaiting user "
            "input.\n"
            f"{outcome}\n"
        )
    body = (
        f"# Research Report: {heading}\n\n"
        "## Research Parameters\n"
        f"- **Topic**: {task.topic}\n"
        f"{f'- **Clarification**: {clarified}' if clarified else ''}\n\n"
        "## Synthesis\n"
        f"This research query was processed through {len(STAGES)} stages:\n"
        f"{stages}\n\n"
        "---\n\n"
        "## About This Demo (SEP-1686: Tasks)\n\n"
        "This tool demonstrates MCP's task-based execution pattern for long-running operations:\n\n"
        "**Task Lifecycle Demonstrated:**\n"
        "1. `tools/call` with `task` parameter → Server returns `CreateTaskResult` (not the final "
        "result)\n"
        "2. Client polls `tasks/get` → Server returns current status and `statusMessage`\n"
        f"3. Status progressed: `working` → {progression}`completed`\n"
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
    return {"content": [text(body)]}


async def _ask_clarification(runner: _Runner, task: ResearchTask) -> None:
    message = f'Found multiple interpretations for "{task.topic}". Requesting clarification...'
    runner.update(task, "input_required", message)
    task.elicitation = {
        "message": f'The research query "{task.topic}" could have multiple interpretations. '
        "Please clarify what you're looking for:",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "interpretation": {
                    "type": "string",
                    "title": "Clarification",
                    "description": "Which interpretation of the topic do you mean?",
                    "oneOf": _interpretations(task.topic),
                }
            },
            "required": ["interpretation"],
        },
        "_meta": {RELATED_TASK: {"taskId": task.task_id}},
    }
    task.answer = asyncio.get_running_loop().create_future()
    runner.update(task, "input_required", message)  # queued for the client: said again
    answer = await _answer_within(runner, task.answer)
    if task.relay is not None and not task.relay.done():
        task.relay.cancel()  # timed out after delivery: the SDK's cancel goes nowhere visible
    task.clarification = _clarification(answer)
    runner.update(task, "working", f'Continuing with interpretation: "{task.clarification}"...')


async def research_query(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``tools/call`` of ``simulate-research-query``: a task, started at once."""
    if not wants_task(ctx):
        message = f"Tool {TOOL} requires task augmentation (taskSupport: 'required')"
        return tool_error(mcp_error_text(METHOD_NOT_FOUND, message))
    args, issues = zod.parse(FIELDS, arguments)
    if issues:
        raise task_creation_error()
    state = client_state(ctx)
    created = ctx.now_ns
    task = ResearchTask(
        task_id=secrets.token_hex(16),
        created_ns=created,
        topic=str(args["topic"]),
        ambiguous=bool(args["ambiguous"]) and "elicitation" in ctx.client_capabilities,
        updated_ns=created,
        expires_ns=created + TTL_MS * NS_PER_MS,
    )
    _tasks(state)[task.task_id] = task
    runtime, session = ctx.runtime, ctx.session
    task.status_message = f"{STAGES[0]}..."  # set before the call is answered, as on the server
    ctx.push("notifications/tasks/status", task.snapshot())
    ctx.background(lambda channel: _resume(_Runner(runtime, session, channel), task))
    return {"task": task.snapshot()}


async def _resume(runner: _Runner, task: ResearchTask) -> None:
    """``runResearchProcess`` after its first status (sent while answering the call)."""
    stage_ns = int(STAGE_SECONDS * NS_PER_S)
    with contextlib.suppress(_TerminalError):
        due = task.created_ns + stage_ns
        await runner.until(due)
        for index, stage in enumerate(STAGES[1:], start=1):
            runner.update(task, "working", f"{stage}...", at_ns=due)
            if index == CLARIFY_AT and task.ambiguous and task.clarification is None:
                await _ask_clarification(runner, task)
                due = runner.now()  # the stage's work starts once the client answered
            due += stage_ns
            await runner.until(due)
        task.result = report(task)
        runner.update(task, "completed", None, at_ns=due)  # storeTaskResult: message stays


async def _answer_within(
    runner: _Runner, answer: asyncio.Future[JsonValue]
) -> JsonValue | BaseException:
    """The client's answer, its refusal, or a timeout after the SDK's 60 s (on the fake clock)."""
    timer: asyncio.Future[object] = asyncio.ensure_future(runner.wait(REQUEST_TIMEOUT_SECONDS))
    racing: set[asyncio.Future[Any]] = {answer, timer}
    try:
        await asyncio.wait(racing, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    if not answer.done():
        answer.cancel()
        return ClientError(REQUEST_TIMEOUT, "Request timed out")
    error = answer.exception()
    return error if error is not None else answer.result()


def _task_id(params: JsonObject) -> str:
    task_id = params.get("taskId", jsfmt.UNDEFINED)
    if not isinstance(task_id, str):
        raise protocol_error([zod.invalid_type("string", task_id, ["params", "taskId"])])
    return task_id


async def get_task(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/get``."""
    task = _live(ctx.runtime, ctx.session, client_state(ctx), _task_id(params))
    if task is None:
        message = mcp_error_text(INVALID_PARAMS, "Failed to retrieve task: Task not found")
        raise RpcError(INVALID_PARAMS, message)
    return task.snapshot()


async def list_tasks(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/list``: ten a page, the cursor being the last task id of the previous page."""
    state = client_state(ctx)
    ids = [
        task_id
        for task_id in list(_tasks(state))
        if _live(ctx.runtime, ctx.session, state, task_id) is not None
    ]
    check_cursor(params)
    cursor = params.get("cursor")
    start = 0
    if cursor:
        if cursor not in ids:
            message = f"Failed to list tasks: Invalid cursor: {jsfmt.js_string(cursor)}"
            raise RpcError(INVALID_PARAMS, mcp_error_text(INVALID_PARAMS, message))
        start = ids.index(str(cursor)) + 1
    page = ids[start : start + PAGE_SIZE]
    answer: JsonObject = {"tasks": [_tasks(state)[task_id].snapshot() for task_id in page]}
    if start + PAGE_SIZE < len(ids):
        answer["nextCursor"] = page[-1]
    answer["_meta"] = {}
    return answer


async def cancel_task(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/cancel``: a running task stops (no status notification); a finished one refuses."""
    task_id = _task_id(params)
    task = _live(ctx.runtime, ctx.session, client_state(ctx), task_id)
    if task is None:
        raise RpcError(INVALID_PARAMS, mcp_error_text(INVALID_PARAMS, f"Task not found: {task_id}"))
    if task.status in TERMINAL:
        message = f"Cannot cancel task in terminal status: {task.status}"
        raise RpcError(INVALID_PARAMS, mcp_error_text(INVALID_PARAMS, message))
    task.status = "cancelled"
    task.status_message = "Client cancelled task execution."
    task.updated_ns = ctx.now_ns
    task.expires_ns = task.updated_ns + TTL_MS * NS_PER_MS
    if task.answer is not None and not task.answer.done():  # the queue is cleared
        task.answer.set_exception(ClientError(INTERNAL_ERROR, "Task cancelled or completed"))
    if task.relay is not None and not task.relay.done():
        task.relay.cancel()
    task.elicitation = None
    task.changed.set()
    return {"_meta": {}, **task.snapshot()}


def _deliver(ctx: RequestContext, task: ResearchTask) -> None:
    """Send the queued elicitation on this ``tasks/result`` stream, without waiting for it.

    The answer reaches the research in the background, as the SDK routes it; a request that
    already timed out is still delivered (it stayed queued), and its answer is ignored.
    """
    request, waiting = task.elicitation, task.answer
    task.elicitation = None
    if request is not None:
        task.relay = asyncio.ensure_future(_relay(ctx, request, waiting))


async def _relay(
    ctx: RequestContext, request: JsonObject, waiting: asyncio.Future[JsonValue] | None
) -> None:
    """Ask the client and hand its answer (or refusal) to the waiting research.

    A request that already timed out is only sent: nobody waits for its answer.
    """
    if waiting is None or waiting.done():
        asking = asyncio.ensure_future(ctx.ask_client("elicitation/create", request, within=1))
        for _ in range(3):  # long enough to send it, then stop waiting (no cancel is sent)
            await asyncio.sleep(0)
        asking.cancel()
        return
    try:
        answer = await ctx.ask_client("elicitation/create", request, within=RELAY_SECONDS)
    except ClientError as error:
        if waiting is not None and not waiting.done():
            waiting.set_exception(error)
        return
    if waiting is not None and not waiting.done():
        waiting.set_result(answer)


async def task_result(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/result``: deliver what the task queued, wait for it to finish, then its result."""
    task_id = _task_id(params)
    state = client_state(ctx)
    while True:
        task = _live(ctx.runtime, ctx.session, state, task_id)
        if task is None:
            raise RpcError(
                INVALID_PARAMS, mcp_error_text(INVALID_PARAMS, f"Task not found: {task_id}")
            )
        if task.elicitation is not None:
            _deliver(ctx, task)
            await asyncio.sleep(0)
        if task.status in TERMINAL:
            break
        await ctx.runtime.wait(ctx.session, POLL_INTERVAL_MS / 1000)
    if task.result is None:
        raise RpcError(INTERNAL_ERROR, f"Task {task_id} has no result stored")
    return {**task.result, "_meta": {RELATED_TASK: {"taskId": task_id}}}


def check_related_task(ctx: RequestContext, params: JsonObject) -> None:
    """A request naming a related task (``_meta``) the client doesn't have is refused (SDK)."""
    meta = params.get("_meta")
    related = meta.get(RELATED_TASK) if isinstance(meta, dict) else None
    task_id = related.get("taskId") if isinstance(related, dict) else None
    if task_id is None:
        return
    if _live(ctx.runtime, ctx.session, client_state(ctx), task_id) is None:
        message = f"Task not found: {jsfmt.js_string(task_id)}"
        raise RpcError(INVALID_PARAMS, mcp_error_text(INVALID_PARAMS, message))
