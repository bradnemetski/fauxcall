"""MCP Tasks (SEP-1686) as ``simulate-research-query`` runs them, per MCP session.

Recorded timeline: the task is created ``working`` with "Gathering sources...", moves to
"Analyzing content...", "Synthesizing findings..." and "Generating report..." about 1.002 s
apart, and completes 4.01 s after creation (the status message stays on the last stage).
``ttl`` is always 300000 ms whatever the client asks; a task is gone 300 s after its last
update. ``tasks/result`` waits for completion; a cancelled task has no result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fauxcall.runtime import RpcError

from fauxcall_everything.jsnum import iso_ms
from fauxcall_everything.zodlike import INTERNAL_ERROR, INVALID_PARAMS

if TYPE_CHECKING:
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

TTL_MS: Final = 300_000
POLL_INTERVAL_MS: Final = 1000
PAGE_SIZE: Final = 10
"""Tasks per ``tasks/list`` page (recorded: 11 tasks came as 10 + 1)."""
STAGES: Final = (
    "Gathering sources...",
    "Analyzing content...",
    "Synthesizing findings...",
    "Generating report...",
)
STAGE_NS: Final = 1_002_000_000
"""Time between two stages (recorded 1.001 to 1.004 s)."""
COMPLETE_NS: Final = 4_010_000_000
"""Time from creation to completion (recorded 4.008 to 4.021 s)."""
CANCELLED_MESSAGE: Final = "Client cancelled task execution."
RELATED_TASK: Final = "io.modelcontextprotocol/related-task"

REPORT: Final = """# Research Report: {topic}

## Research Parameters
- **Topic**: {topic}


## Synthesis
This research query was processed through 4 stages:
- Stage 1: Gathering sources ✓
- Stage 2: Analyzing content ✓
- Stage 3: Synthesizing findings ✓
- Stage 4: Generating report ✓

---

## About This Demo (SEP-1686: Tasks)

This tool demonstrates MCP's task-based execution pattern for long-running operations:

**Task Lifecycle Demonstrated:**
1. `tools/call` with `task` parameter → Server returns `CreateTaskResult` (not the final result)
2. Client polls `tasks/get` → Server returns current status and `statusMessage`
3. Status progressed: `working` → `completed`
4. Client calls `tasks/result` → Server returns this final result


**Key Concepts:**
- Tasks enable "call now, fetch later" patterns
- `statusMessage` provides human-readable progress updates
- Tasks have TTL (time-to-live) for automatic cleanup
- `pollInterval` suggests how often to check status
- Elicitation requests use `relatedTask` to queue via tasks/result (works on all transports)

*This is a simulated research report from the Everything MCP Server.*
"""


@dataclass(slots=True)
class Task:
    """One research task."""

    task_id: str
    topic: str
    created_ns: int
    complete_ns: int
    """When it completes: 4.01 s after creation, or earlier when a fast fake skipped the wait."""
    cancelled_ns: int | None = None

    def status(self, now_ns: int) -> str:
        """``working``, ``completed`` or ``cancelled`` at ``now_ns``."""
        if self.cancelled_ns is not None:
            return "cancelled"
        return "completed" if now_ns >= self.complete_ns else "working"

    def updated_ns(self, now_ns: int) -> int:
        """When the task last changed (stage, completion or cancellation)."""
        if self.cancelled_ns is not None:
            return self.cancelled_ns
        if now_ns >= self.complete_ns:
            return self.complete_ns
        elapsed = max(now_ns - self.created_ns, 0)
        return self.created_ns + min(elapsed // STAGE_NS, len(STAGES) - 1) * STAGE_NS

    def message(self, now_ns: int) -> str:
        """The ``statusMessage`` at ``now_ns``."""
        if self.cancelled_ns is not None:
            return CANCELLED_MESSAGE
        elapsed = max(now_ns - self.created_ns, 0)
        return STAGES[min(elapsed // STAGE_NS, len(STAGES) - 1)]

    def expired(self, now_ns: int) -> bool:
        """Whether the store has dropped it (``ttl`` after its last update)."""
        return now_ns - self.updated_ns(now_ns) > TTL_MS * 1_000_000

    def view(self, now_ns: int) -> JsonObject:
        """The task as ``tasks/get`` and ``tasks/list`` show it."""
        return {
            "taskId": self.task_id,
            "status": self.status(now_ns),
            "ttl": TTL_MS,
            "createdAt": iso_ms(self.created_ns),
            "lastUpdatedAt": iso_ms(self.updated_ns(now_ns)),
            "pollInterval": POLL_INTERVAL_MS,
            "statusMessage": self.message(now_ns),
        }


def _store(ctx: RequestContext) -> dict[str, Task]:
    """This MCP session's tasks, in creation order."""
    sessions: dict[str, dict[str, Task]] = ctx.state.setdefault("tasks", {})
    return sessions.setdefault(ctx.mcp_session or "", {})


def _live(ctx: RequestContext, task_id: str) -> Task | None:
    task = _store(ctx).get(task_id)
    if task is None or task.expired(ctx.now_ns):
        return None
    return task


def create(ctx: RequestContext, topic: str) -> JsonObject:
    """Start a task (the ``CreateTaskResult`` of ``tools/call`` with ``task``)."""
    task = Task(f"{ctx.rng.getrandbits(128):032x}", topic, ctx.now_ns, ctx.now_ns + COMPLETE_NS)
    _store(ctx)[task.task_id] = task
    return {"task": task.view(ctx.now_ns)}


def get(ctx: RequestContext, task_id: str) -> JsonObject:
    """``tasks/get``."""
    task = _live(ctx, task_id)
    if task is None:
        raise RpcError(INVALID_PARAMS, "MCP error -32602: Failed to retrieve task: Task not found")
    return task.view(ctx.now_ns)


def cancel(ctx: RequestContext, task_id: str) -> JsonObject:
    """``tasks/cancel``: only a working task can be cancelled."""
    task = _live(ctx, task_id)
    if task is None:
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Task not found: {task_id}")
    status = task.status(ctx.now_ns)
    if status != "working":
        raise RpcError(
            INVALID_PARAMS, f"MCP error -32602: Cannot cancel task in terminal status: {status}"
        )
    task.cancelled_ns = ctx.now_ns
    return {"_meta": {}, **task.view(ctx.now_ns)}


async def result(ctx: RequestContext, task_id: str) -> JsonValue:
    """``tasks/result``: waits for a working task to complete, as the real server does."""
    task = _live(ctx, task_id)
    if task is None:
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Task not found: {task_id}")
    now_ns = ctx.now_ns
    if task.status(now_ns) == "working":
        await ctx.pause((task.complete_ns - now_ns) / 1e9)
        now_ns = ctx.runtime.clocks.for_session(ctx.session).now_ns()
        if task.status(now_ns) == "working":  # fast mode or a frozen clock: it finished now
            task.complete_ns = now_ns
    if task.status(now_ns) == "cancelled":
        raise RpcError(INTERNAL_ERROR, f"Task {task_id} has no result stored")
    return {
        "content": [{"type": "text", "text": REPORT.format(topic=task.topic)}],
        "_meta": {RELATED_TASK: {"taskId": task_id}},
    }


def listing(ctx: RequestContext, params: JsonObject) -> JsonObject:
    """``tasks/list``: this session's live tasks, 10 per page; the cursor is the last task id."""
    now_ns = ctx.now_ns
    live = [task for task in _store(ctx).values() if not task.expired(now_ns)]
    start = 0
    if "cursor" in params:
        cursor = params["cursor"]
        position = next((i for i, task in enumerate(live) if task.task_id == cursor), None)
        if position is None:
            raise RpcError(
                INVALID_PARAMS, f"MCP error -32602: Failed to list tasks: Invalid cursor: {cursor}"
            )
        start = position + 1
    page = live[start : start + PAGE_SIZE]
    body: JsonObject = {"tasks": [task.view(now_ns) for task in page]}
    if start + PAGE_SIZE < len(live):
        body["nextCursor"] = page[-1].task_id
    body["_meta"] = {}
    return body
