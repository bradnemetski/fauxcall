"""``simulate-research-query`` tasks and the ``tasks/*`` methods that report them.

Recorded timeline: four one-second stages ("Gathering sources..." …), ``completed`` four seconds
after creation; an ambiguous topic (for a client that can elicit) stops at two seconds in
``input_required`` until ``tasks/result`` delivers the clarification request, then finishes two
seconds after the answer. ``tasks/result`` checks once a second, so it answers on the first
whole second (from its own start) at which the task is done.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fauxcall.runtime import ClientError, RpcError

from fauxcall_everything import jsfmt
from fauxcall_everything.state import ResearchTask, iso_ms, mcp_session

if TYPE_CHECKING:
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

SECOND: Final = 1_000_000_000
STAGES: Final = (
    "Gathering sources",
    "Analyzing content",
    "Synthesizing findings",
    "Generating report",
)
TTL_MS: Final = 300000
QUESTION_AT: Final = 2
"""The stage at which an ambiguous topic asks for clarification."""
RELATED: Final = "io.modelcontextprotocol/related-task"
MAX_WAIT_SECONDS: Final = TTL_MS // 1000
PAGE_SIZE: Final = 10


def status_at(task: ResearchTask, now: int) -> tuple[str, str, int]:
    """The task's status, status message and last update at ``now`` (fake clock, ns)."""
    if task.cancelled_ns is not None and now >= task.cancelled_ns:
        return "cancelled", "Client cancelled task execution.", task.cancelled_ns
    done = completion_ns(task)
    if task.completed_ns is not None or (done is not None and now >= done):
        finished = task.completed_ns if task.completed_ns is not None else done
        assert finished is not None  # noqa: S101
        return "completed", f"{STAGES[-1]}...", finished
    elapsed = max(now - task.created_ns, 0)
    stage = min(elapsed // SECOND, len(STAGES) - 1)
    if not task.ambiguous or stage < QUESTION_AT:
        return "working", f"{STAGES[stage]}...", task.created_ns + stage * SECOND
    if task.answered_ns is None or now < task.answered_ns:
        message = f'Found multiple interpretations for "{task.topic}". Requesting clarification...'
        return "input_required", message, task.created_ns + QUESTION_AT * SECOND
    if now < task.answered_ns + SECOND:
        message = f'Continuing with interpretation: "{task.clarification}"...'
        return "working", message, task.answered_ns
    return "working", f"{STAGES[-1]}...", task.answered_ns + SECOND


def completion_ns(task: ResearchTask) -> int | None:
    """When the task completes (``None``: it waits for a clarification)."""
    if task.completed_ns is not None:
        return task.completed_ns
    if not task.ambiguous:
        return task.created_ns + len(STAGES) * SECOND
    if task.answered_ns is None:
        return None
    return task.answered_ns + (len(STAGES) - QUESTION_AT) * SECOND


def describe(task: ResearchTask, now: int) -> JsonObject:
    """The task object as tasks/get, tasks/list and the CreateTaskResult show it."""
    status, message, updated = status_at(task, now)
    return {
        "taskId": task.task_id,
        "status": status,
        "ttl": TTL_MS,
        "createdAt": iso_ms(task.created_ns),
        "lastUpdatedAt": iso_ms(updated),
        "pollInterval": 1000,
        "statusMessage": message,
    }


def report(task: ResearchTask) -> str:
    """The final research report (markdown)."""
    clarification = task.clarification
    topic = f"{task.topic} ({clarification})" if clarification else task.topic
    stages = "\n".join(f"- Stage {i}: {stage} ✓" for i, stage in enumerate(STAGES, start=1))
    progressed = "`input_required` → `working` → " if clarification else ""
    flow = ""
    if clarification:
        ending = (
            "**Note:** Elicitation failed and a default interpretation was used."
            if "unavailable" in clarification
            else f'After receiving clarification ("{clarification}"), the task resumed '
            "processing and completed."
        )
        flow = (
            "**Elicitation Flow:**\n"
            "When the query was ambiguous, the server sent an `elicitation/create` request\n"
            "to the client. The task status changed to `input_required` while awaiting user "
            f"input.\n{ending}\n"
        )
    clarified = f"- **Clarification**: {clarification}" if clarification else ""
    return (
        f"# Research Report: {topic}\n\n"
        "## Research Parameters\n"
        f"- **Topic**: {task.topic}\n"
        f"{clarified}\n\n"
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


def _task_id(params: JsonObject) -> str:
    task_id = params.get("taskId")
    if not isinstance(task_id, str):
        got = jsfmt.received(task_id, present="taskId" in params)
        raise RpcError(-32603, jsfmt.issue_json(["params", "taskId"], "string", got))
    return task_id


def expires_ns(task: ResearchTask) -> int:
    """When the task store forgets the task: its ttl after creation, restarted when it ends."""
    created_limit = task.created_ns + TTL_MS * 1_000_000
    ended = task.cancelled_ns if task.cancelled_ns is not None else completion_ns(task)
    if ended is not None and ended < created_limit:
        return ended + TTL_MS * 1_000_000
    return created_limit


def live_tasks(ctx: RequestContext, now: int) -> dict[str, ResearchTask]:
    """This client's tasks, without those the store has forgotten by ``now``."""
    tasks = mcp_session(ctx).tasks
    for task_id in [i for i, task in tasks.items() if now >= expires_ns(task)]:
        del tasks[task_id]
    return tasks


def _find(ctx: RequestContext, params: JsonObject, missing: str) -> ResearchTask:
    task_id = _task_id(params)
    task = live_tasks(ctx, ctx.now_ns).get(task_id)
    if task is None:
        raise RpcError(-32602, missing.format(task_id=task_id))
    return task


async def tasks_get(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/get``."""
    task = _find(ctx, params, "MCP error -32602: Failed to retrieve task: Task not found")
    return describe(task, ctx.now_ns)


async def tasks_list(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/list``: this client's tasks, oldest first, ten a page.

    The cursor is the last task id of the previous page (recording 000512).
    """
    ids = list(live_tasks(ctx, ctx.now_ns))
    cursor = params.get("cursor")
    start = 0
    if cursor:
        if cursor not in ids:
            raise RpcError(
                -32602, f"MCP error -32602: Failed to list tasks: Invalid cursor: {cursor}"
            )
        start = ids.index(str(cursor)) + 1
    page = ids[start : start + PAGE_SIZE]
    tasks = mcp_session(ctx).tasks
    listed: list[JsonValue] = [describe(tasks[task_id], ctx.now_ns) for task_id in page]
    body: JsonObject = {"tasks": listed}
    if start + PAGE_SIZE < len(ids):
        body["nextCursor"] = page[-1]
    body["_meta"] = {}
    return body


async def tasks_cancel(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/cancel``: only a task that hasn't ended."""
    task = _find(ctx, params, "MCP error -32602: Task not found: {task_id}")
    status, _, _ = status_at(task, ctx.now_ns)
    if status in {"completed", "cancelled", "failed"}:
        raise RpcError(-32602, f"MCP error -32602: Cannot cancel task in terminal status: {status}")
    task.cancelled_ns = ctx.now_ns
    return {"_meta": {}, **describe(task, ctx.now_ns)}


async def _clarify(ctx: RequestContext, task: ResearchTask) -> str:
    """Ask the client which interpretation it means (on this tasks/result stream)."""
    if "python" in task.topic.lower():
        options: list[JsonValue] = [
            {"const": "programming", "title": "Python programming language"},
            {"const": "snake", "title": "Python snake species"},
            {"const": "comedy", "title": "Monty Python comedy group"},
        ]
    else:
        options = [
            {"const": "technical", "title": "Technical/scientific perspective"},
            {"const": "historical", "title": "Historical perspective"},
            {"const": "current", "title": "Current events/news perspective"},
        ]
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
                    "oneOf": options,
                }
            },
            "required": ["interpretation"],
        },
        "_meta": {RELATED: {"taskId": task.task_id}},
    }
    try:
        answer = await ctx.ask_client("elicitation/create", params)
    except ClientError:
        return "technical (default - elicitation unavailable)"
    found = answer if isinstance(answer, dict) else {}
    content = found.get("content")
    if found.get("action") == "accept" and isinstance(content, dict):
        chosen = content.get("interpretation")
        return str(chosen) if chosen else "User accepted without selection"
    if found.get("action") == "decline":
        return "User declined - using default interpretation"
    return "User cancelled - using default interpretation"


async def tasks_result(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/result``: wait (checking once a second) for the result, asking what it must."""
    task = _find(ctx, params, "MCP error -32602: Task not found: {task_id}")
    moment = ctx.now_ns
    for tick in range(MAX_WAIT_SECONDS + 1):
        if tick:
            await ctx.pause(1.0)
            moment += SECOND
        status, _, _ = status_at(task, moment)
        if status == "cancelled":
            raise RpcError(-32603, f"Task {task.task_id} has no result stored")
        if status == "completed":
            task.completed_ns = completion_ns(task)
            break
        if status == "input_required" and task.answered_ns is None:
            task.clarification = await _clarify(ctx, task)
            task.answered_ns = moment
    else:  # the store forgot the task while waiting (its ttl ran out)
        raise RpcError(-32602, f"MCP error -32602: Task not found: {task.task_id}")
    return {
        "content": [{"type": "text", "text": report(task)}],
        "_meta": {RELATED: {"taskId": task.task_id}},
    }
