"""simulate-research-query's tasks (SEP-1686), on the fake's clock.

The real tool creates a task (ttl 300000 ms whatever the client asks, poll interval 1000 ms) and
works through four stages of one second each (plus ~3 ms of bookkeeping per stage, as recorded):
``Gathering sources...`` -> ``Analyzing content...`` -> ``Synthesizing findings...`` ->
``Generating report...`` -> completed. An ambiguous query from a client that can elicit stops at
the third stage in ``input_required`` and queues an ``elicitation/create`` that only
``tasks/result`` delivers; the answer resumes the task, which completes two stages later. A
queued question times out after the SDK's 60 s and the task goes on with a default reading.

The SDK's ``tasks/result`` checks the task once a second from the moment it is called and
answers at the first check that finds it terminal (recordings 000289, 000322, 000324, 000399,
000404); every status change is pushed to the client's GET stream as
``notifications/tasks/status`` (000411), except a cancellation, which the SDK makes in its store
directly. A task is forgotten 300 s after it ends (or after it was created, if it never ends).
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from fauxcall.clock import ns_to_datetime
from fauxcall.runtime import ClientError, RpcError

from fauxcall_everything.jsvalues import template, truthy
from fauxcall_everything.zodish import INTERNAL_ERROR, INVALID_PARAMS, elicit_result

if TYPE_CHECKING:
    from collections.abc import Callable

    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.types import JsonObject, JsonValue

STAGES: Final = (
    "Gathering sources",
    "Analyzing content",
    "Synthesizing findings",
    "Generating report",
)
STAGE_NS: Final = 1_003_000_000
TICK_NS: Final = 1_000_000_000
TTL_MS: Final = 300_000
POLL_INTERVAL_MS: Final = 1000
ELICITATION_TIMEOUT_NS: Final = 60_000_000_000
RELATED_TASK: Final = "io.modelcontextprotocol/related-task"
CANCELLED_MESSAGE: Final = "Client cancelled task execution."
UNAVAILABLE: Final = "technical (default - elicitation unavailable)"
REPORT_FAILURE: Final = "TypeError: state.clarification.includes is not a function"
"""How the report fails for a clarification that is neither text nor a list (000505)."""
TERMINAL: Final = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class Status:
    """One status the task goes through."""

    at_ns: int
    status: str
    message: str


@dataclass(slots=True)
class ResearchTask:
    """One task of simulate-research-query."""

    task_id: str
    topic: str
    ambiguous: bool
    """Asked ambiguous by a client that can elicit: the task will need a clarification."""
    created_ns: int
    clarification: str | None = None
    resumed_ns: int | None = None
    cancelled_ns: int | None = None
    asked: bool = False
    """The clarification question went out (through tasks/result)."""
    fails: bool = False
    """The clarification makes the report throw (a number or boolean): the task fails."""
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    horizon_ns: int = 0
    """The latest moment a tasks/result has seen (in fast mode it runs ahead of the clock)."""

    @staticmethod
    def new(topic: str, *, ambiguous: bool, now_ns: int) -> ResearchTask:
        """A task created now, with an id like the SDK's (16 random bytes, hex)."""
        return ResearchTask(secrets.token_hex(16), topic, ambiguous, now_ns)

    @property
    def asks_at_ns(self) -> int:
        """When an ambiguous task stops for its clarification."""
        return self.created_ns + 2 * STAGE_NS

    def time(self, now_ns: int) -> int:
        """The moment to judge the task at: now, or later if a tasks/result already saw later."""
        return max(now_ns, self.horizon_ns)

    def settle(self, now_ns: int) -> None:
        """Let a clarification nobody answered time out, as the SDK's request does."""
        if self.ambiguous and self.resumed_ns is None:
            deadline = self.asks_at_ns + ELICITATION_TIMEOUT_NS
            if now_ns >= deadline:
                self.resume(UNAVAILABLE, deadline)

    def resume(self, clarification: str, at_ns: int, *, fails: bool = False) -> None:
        """The clarification arrived: the task works on."""
        self.clarification = clarification
        self.fails = fails
        self.resumed_ns = at_ns
        self.answered.set()

    def statuses(self) -> list[Status]:
        """Every status known so far, in order (the pushes the real server sends)."""
        start = self.created_ns
        found = [Status(start + i * STAGE_NS, "working", f"{STAGES[i]}...") for i in range(3)]
        if not self.ambiguous:
            found.append(Status(start + 3 * STAGE_NS, "working", f"{STAGES[3]}..."))
            found.append(Status(start + 4 * STAGE_NS, "completed", f"{STAGES[3]}..."))
        else:
            asking = (
                f'Found multiple interpretations for "{self.topic}". Requesting clarification...'
            )
            found += [Status(self.asks_at_ns, "input_required", asking)] * 2
            if self.resumed_ns is not None:
                resumed = self.resumed_ns
                found.append(
                    Status(
                        resumed,
                        "working",
                        f'Continuing with interpretation: "{self.clarification}"...',
                    )
                )
                found.append(Status(resumed + STAGE_NS, "working", f"{STAGES[3]}..."))
                if self.fails:
                    found.append(Status(resumed + 2 * STAGE_NS, "failed", REPORT_FAILURE))
                else:
                    found.append(Status(resumed + 2 * STAGE_NS, "completed", f"{STAGES[3]}..."))
        if self.cancelled_ns is not None:
            found = [s for s in found if s.at_ns < self.cancelled_ns]
        return found

    def at(self, now_ns: int) -> Status:
        """The task's status at ``now_ns``."""
        if self.cancelled_ns is not None and now_ns >= self.cancelled_ns:
            return Status(self.cancelled_ns, "cancelled", CANCELLED_MESSAGE)
        statuses = self.statuses()
        known = [s for s in statuses if s.at_ns <= now_ns]
        return known[-1] if known else statuses[0]

    def ended_ns(self) -> int | None:
        """When the task became terminal (``None``: not yet known)."""
        if self.cancelled_ns is not None:
            return self.cancelled_ns
        last = self.statuses()[-1]
        return last.at_ns if last.status in TERMINAL else None

    def gone(self, now_ns: int) -> bool:
        """Whether the store forgot the task (its TTL ran out)."""
        ttl = TTL_MS * 1_000_000
        ended = self.ended_ns()
        expires = (
            ended + ttl
            if ended is not None and ended <= self.created_ns + ttl
            else self.created_ns + ttl
        )
        return now_ns >= expires

    def to_json(self, status: Status) -> JsonObject:
        """The task as tasks/get, tasks/list and status notifications show it."""
        return {
            "taskId": self.task_id,
            "status": status.status,
            "ttl": TTL_MS,
            "createdAt": iso_ms(self.created_ns),
            "lastUpdatedAt": iso_ms(status.at_ns),
            "pollInterval": POLL_INTERVAL_MS,
            "statusMessage": status.message,
        }

    def report(self) -> JsonObject:
        """The task's result (tasks/result), with its related-task metadata."""
        return {
            "content": [{"type": "text", "text": research_report(self.topic, self.clarification)}],
            "_meta": {RELATED_TASK: {"taskId": self.task_id}},
        }

    def question(self) -> JsonObject:
        """The elicitation/create the task queues for its clarification."""
        return {
            "message": (
                f'The research query "{self.topic}" could have multiple interpretations. '
                "Please clarify what you're looking for:"
            ),
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "interpretation": {
                        "type": "string",
                        "title": "Clarification",
                        "description": "Which interpretation of the topic do you mean?",
                        "oneOf": interpretations(self.topic),
                    }
                },
                "required": ["interpretation"],
            },
            "_meta": {RELATED_TASK: {"taskId": self.task_id}},
        }


def iso_ms(ns: int) -> str:
    """``new Date().toISOString()``."""
    return ns_to_datetime(ns).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def interpretations(topic: str) -> list[JsonValue]:
    """The readings the server offers for an ambiguous topic."""
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


def research_report(topic: str, clarification: str | None) -> str:
    """The report simulate-research-query produces (generateResearchReport)."""
    titled = f"{topic} ({clarification})" if clarification else topic
    clarified = f"- **Clarification**: {clarification}" if clarification else ""
    stages = "\n".join(f"- Stage {i + 1}: {stage} ✓" for i, stage in enumerate(STAGES))
    progressed = "`input_required` → `working` → " if clarification else ""
    flow = ""
    if clarification:
        outcome = (
            "**Note:** Elicitation failed and a default interpretation was used."
            if "unavailable" in clarification
            else f'After receiving clarification ("{clarification}"), '
            "the task resumed processing and completed."
        )
        flow = (
            "**Elicitation Flow:**\n"
            "When the query was ambiguous, the server sent an `elicitation/create` request\n"
            "to the client. "
            "The task status changed to `input_required` while awaiting user input.\n"
            f"{outcome}\n"
        )
    return (
        f"# Research Report: {titled}\n\n"
        "## Research Parameters\n"
        f"- **Topic**: {topic}\n"
        f"{clarified}\n\n"
        "## Synthesis\n"
        f"This research query was processed through {len(STAGES)} stages:\n"
        f"{stages}\n\n"
        "---\n\n"
        "## About This Demo (SEP-1686: Tasks)\n\n"
        "This tool demonstrates MCP's task-based execution pattern for long-running operations:\n\n"
        "**Task Lifecycle Demonstrated:**\n"
        "1. `tools/call` with `task` parameter → "
        "Server returns `CreateTaskResult` (not the final result)\n"
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


def clarification_of(reply: JsonValue) -> tuple[str, bool]:
    """How the task reads the client's answer: (clarification, whether the report will fail).

    The report calls ``clarification.includes(...)``: a number or a boolean has no such method.
    """
    parsed, issues = elicit_result(reply)
    if issues:
        return UNAVAILABLE, False
    content = parsed.get("content")
    if parsed.get("action") == "accept" and truthy(content):
        chosen = content.get("interpretation") if isinstance(content, dict) else None
        if not truthy(chosen):
            return "User accepted without selection", False
        return template(chosen), not isinstance(chosen, str | list)
    if parsed.get("action") == "decline":
        return "User declined - using default interpretation", False
    return "User cancelled - using default interpretation", False


async def push_statuses(
    task: ResearchTask,
    clock: Callable[[], int],
    channel: Channel,
    current: Callable[[], bool] = lambda: True,
) -> None:
    """Push each status change to the client's GET stream as it happens (background work).

    ``current`` turns false when a reset of the fake forgot the task: the pushes stop.
    """
    sent = 0
    while current():
        now = clock()
        task.settle(now)
        statuses = task.statuses()
        while sent < len(statuses) and statuses[sent].at_ns <= now:
            channel.push("notifications/tasks/status", task.to_json(statuses[sent]))
            sent += 1
        if task.cancelled_ns is not None and now >= task.cancelled_ns:
            return
        upcoming = [s.at_ns for s in statuses[sent:]]
        if task.cancelled_ns is not None:  # cancelled ahead of the clock: stop there
            upcoming.append(task.cancelled_ns)
        if upcoming:
            await channel.pause(max(min(upcoming) - now, 1_000_000) / 1e9)
            continue
        if statuses[-1].status in TERMINAL:
            return
        # waiting for the clarification: until it arrives or the question times out
        deadline = task.asks_at_ns + ELICITATION_TIMEOUT_NS
        try:
            async with asyncio.timeout(max(deadline - now, 1_000_000) / 1e9):
                await task.answered.wait()
        except TimeoutError:
            continue


async def task_result(ctx: RequestContext, task: ResearchTask) -> JsonObject:
    """tasks/result: deliver the queued question, then answer once the task is terminal."""
    started = ctx.now_ns
    tick = 0
    while True:
        at = started + tick * TICK_NS
        task.settle(at)
        if task.gone(at):
            raise RpcError(INVALID_PARAMS, f"MCP error -32602: Task not found: {task.task_id}")
        status = task.at(at)
        if (
            task.ambiguous
            and not task.asked
            and at >= task.asks_at_ns
            and task.cancelled_ns is None
        ):
            task.asked = True
            if task.resumed_ns is None:
                await _ask(ctx, task, at)
            else:  # the question timed out but stayed queued: delivered, its answer ignored
                with contextlib.suppress(ClientError):
                    await ctx.ask_client("elicitation/create", task.question(), within=0.001)
            continue
        task.horizon_ns = max(task.horizon_ns, at)
        if status.status in {"cancelled", "failed"}:
            raise RpcError(INTERNAL_ERROR, f"Task {task.task_id} has no result stored")
        if status.status == "completed":
            return task.report()
        await _wait_for_tick(ctx, started + (tick + 1) * TICK_NS)
        tick += 1


async def _ask(ctx: RequestContext, task: ResearchTask, at_ns: int) -> None:
    """Send the clarification question on the tasks/result stream; resume with the answer."""
    deadline = task.asks_at_ns + ELICITATION_TIMEOUT_NS
    try:
        reply = await ctx.ask_client(
            "elicitation/create", task.question(), within=(deadline - at_ns) / 1e9
        )
        clarification, fails = clarification_of(reply)
    except ClientError:
        clarification, fails = UNAVAILABLE, False
    now = ctx.runtime.clocks.for_session(ctx.session).now_ns()
    task.resume(clarification, max(now, at_ns) + 1_000_000, fails=fails)


async def _wait_for_tick(ctx: RequestContext, tick_ns: int) -> None:
    """Sleep until the next check of the task (skipped in fast mode, as every ctx.pause is)."""
    now = ctx.runtime.clocks.for_session(ctx.session).now_ns()
    await ctx.pause(max(0.0, (tick_ns - now) / 1e9))


def page(tasks: list[ResearchTask], cursor: JsonValue, now_ns: int) -> JsonObject:
    """tasks/list: ten per page, the cursor being the last task id of the previous page."""
    alive = [t for t in tasks if not t.gone(t.time(now_ns))]
    ids = [t.task_id for t in alive]
    start = 0
    if cursor:
        if cursor not in ids:
            raise RpcError(
                INVALID_PARAMS,
                f"MCP error -32602: Failed to list tasks: Invalid cursor: {template(cursor)}",
            )
        start = ids.index(str(cursor)) + 1
    chosen = alive[start : start + 10]
    result: JsonObject = {"tasks": [t.to_json(t.at(t.time(now_ns))) for t in chosen]}
    if start + 10 < len(alive):
        result["nextCursor"] = chosen[-1].task_id
    result["_meta"] = {}
    return result
