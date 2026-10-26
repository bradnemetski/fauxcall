"""What every handler shares: per-client state, log messages, the server's clock and tool plumbing.

The real server builds one MCP server per client session (``createServer()`` per
``Mcp-Session-Id``), so everything a client turns on (logging, subscriptions, gzip resources,
tasks, roots) is that client's alone. Here it lives in :class:`ClientState`, one per MCP session
inside FauxCall's per-evaluation ``ctx.state``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from fauxcall.runtime import RpcError

from fauxcall_everything import jsfmt, zod

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable, Sequence

    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.runtime.app import FakeRuntime
    from fauxcall.types import JsonObject, JsonValue

LEVELS: Final = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")
"""RFC 5424 levels, least severe first (the SDK's ``LOG_LEVEL_SEVERITY``)."""
SERVER_ZONE: Final = ZoneInfo("America/New_York")
"""The real server's local zone: ``toLocaleTimeString()`` wrote 21:14:53Z as 5:14:53 PM."""
NS_PER_MS: Final = 1_000_000
NS_PER_S: Final = 1_000_000_000
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
METHOD_NOT_FOUND: Final = -32601
TASK_CREATION_ISSUES: Final = [
    zod.Issue(
        {
            "expected": "object",
            "code": "invalid_type",
            "message": "Invalid input: expected object, received undefined",
        },
        ["task"],
    )
]
"""What the SDK says when a ``task``-augmented call doesn't answer with a task."""


@dataclass(slots=True)
class SessionResource:
    """A resource a client created with ``gzip-file-as-resource``."""

    uri: str
    name: str
    mime_type: str
    blob: str


@dataclass(slots=True)
class ClientState:
    """Everything one connected client (one MCP session) has changed on the server."""

    session: str
    level: str | None = None
    logging: asyncio.Task[None] | None = None
    updates: asyncio.Task[None] | None = None
    subscriptions: dict[str, bool] = field(default_factory=dict)
    resources: dict[str, SessionResource] = field(default_factory=dict)
    roots: list[JsonObject] | None = None
    roots_listening: bool = False
    initialized: bool = False
    channel: Channel | None = None
    """The client's channel (its GET stream), for messages another session's work sends it."""
    issued_url_elicitations: set[str] = field(default_factory=set)
    tasks: dict[str, Any] = field(default_factory=dict)


def client_state(ctx: RequestContext) -> ClientState:
    """The calling client's state (created on first use)."""
    clients: dict[str, ClientState] = ctx.state.setdefault("clients", {})
    key = ctx.mcp_session or ""
    state = clients.get(key)
    if state is None:
        state = clients[key] = ClientState(session=key)
    if ctx.channel is not None:
        state.channel = ctx.channel
    return state


def log_message(
    push: Callable[[str, JsonValue], object],
    state: ClientState,
    level: str,
    data: str,
    logger: str | None = None,
) -> None:
    """``server.sendLoggingMessage``: a log notification, unless below the client's level."""
    if state.level is not None and LEVELS.index(level) < LEVELS.index(state.level):
        return
    params: JsonObject = {"level": level}
    if logger is not None:
        params["logger"] = logger
    params["data"] = data
    push("notifications/message", params)


def now_ns(runtime: FakeRuntime, session: str) -> int:
    """The fake clock of an evaluation session, now."""
    return runtime.clocks.for_session(session).now_ns()


def iso_ms(ns: int) -> str:
    """``new Date().toISOString()``."""
    moment = datetime.fromtimestamp(ns // NS_PER_MS / 1000, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def locale_time(ns: int) -> str:
    """``new Date().toLocaleTimeString()`` on the real server (en-US, its local zone)."""
    moment = datetime.fromtimestamp(ns / NS_PER_S, SERVER_ZONE)
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment:%M:%S} {'AM' if moment.hour < 12 else 'PM'}"  # noqa: PLR2004


def text(value: str) -> JsonObject:
    """A text content block."""
    return {"type": "text", "text": value}


def tool_error(message: str) -> JsonObject:
    """The SDK's ``createToolError``: what a tool that threw answers."""
    return {"content": [text(message)], "isError": True}


def mcp_error_text(code: int, message: str) -> str:
    """An ``McpError``'s message: ``MCP error <code>: <message>``."""
    return f"MCP error {code}: {message}"


def request_params(ctx: RequestContext) -> JsonObject:
    """The params of the JSON-RPC request being answered."""
    message = ctx.json_body if isinstance(ctx.json_body, dict) else {}
    params = message.get("params")
    return params if isinstance(params, dict) else {}


def wants_task(ctx: RequestContext) -> bool:
    """Whether the call is task-augmented (``!!request.params.task``: any object, even ``{}``)."""
    task = request_params(ctx).get("task")
    return isinstance(task, dict) or (task is not None and task is not False and bool(task))


def task_creation_error() -> RpcError:
    """A task-augmented call to a tool that answers with a plain result."""
    return RpcError(
        INVALID_PARAMS,
        mcp_error_text(
            INVALID_PARAMS, "Invalid task creation result: " + zod.issues_json(TASK_CREATION_ISSUES)
        ),
    )


def protocol_error(issues: Sequence[zod.Issue]) -> RpcError:
    """A request the SDK's schema refuses before any handler: ``-32603`` with the issues."""
    return RpcError(INTERNAL_ERROR, zod.issues_json(issues))


def validated_tool(
    name: str, fields: Sequence[zod.Field]
) -> Callable[
    [Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]],
    Callable[[RequestContext, JsonObject], Awaitable[JsonValue]],
]:
    """Wrap a tool as the SDK runs it: validate, run, then refuse a ``task`` it can't create.

    Invalid arguments answer with the SDK's ``isError`` result; an exception the tool raises
    (:class:`ToolError`) answers with its message. A task-augmented call still runs the tool
    (the real one does, side effects included) and then fails as the SDK fails it.
    """

    def wrap(
        body: Callable[[RequestContext, JsonObject], Awaitable[JsonValue]],
    ) -> Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]:
        async def tool(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
            data, issues = zod.parse(fields, arguments)
            if issues:
                answer: JsonValue = tool_error(zod.tool_validation_error(name, issues))
            else:
                try:
                    answer = await body(ctx, data)
                except ToolError as failure:
                    answer = tool_error(str(failure))
            if wants_task(ctx):
                raise task_creation_error()
            return answer

        tool.__name__ = body.__name__
        tool.__doc__ = body.__doc__
        return tool

    return wrap


class ToolError(Exception):
    """A tool threw (``throw new Error(message)``): the call answers ``isError``, its message."""


def js_num(value: JsonValue) -> int | float:
    """A JSON number argument as the server writes it back (``2.0`` -> ``2``)."""
    return jsfmt.json_number(jsfmt.to_float(value))


def check_cursor(params: JsonObject) -> None:
    """A listing's ``cursor`` must be text (``CursorSchema``): refused by the schema otherwise."""
    if "cursor" in params and not isinstance(params["cursor"], str):
        raise protocol_error([zod.invalid_type("string", params["cursor"], ["params", "cursor"])])
