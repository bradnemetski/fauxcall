"""Custom behavior of the Everything Reference Server fake.

The real server (``@modelcontextprotocol/server-everything``) computes nearly every answer from
its arguments, the clock and the MCP session, so its tools, resources, prompts, completions and
tasks are reproduced here in code; ``get-env``, ``get-tiny-image``, the static documents and the
listings replay the recordings. What each piece is based on (recordings, or the server's published
source where recording was impossible) is noted in ``data/model.json`` ``notes`` and the README.

State lives per MCP session (``Mcp-Session-Id``), as on the real server: session resources made
by ``gzip-file-as-resource``, subscriptions, the logging level, the two simulation toggles and
research tasks.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import struct
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import unquote_to_bytes
from zoneinfo import ZoneInfo

from fauxcall.runtime import EarlyResponse, RpcError
from starlette.responses import JSONResponse

from fauxcall_everything import zodlike
from fauxcall_everything.jsnum import as_double, is_positive_integer, js_number, js_string
from fauxcall_everything.zodlike import Field

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from fastapi import Response, WebSocket
    from fauxcall.runtime import RequestContext
    from fauxcall.runtime.mcp_server import Channel
    from fauxcall.types import JsonObject, JsonValue

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

METHOD_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP method -> custom handler of its params (``None``: the recordings answer)."""

type ToolHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]

# -- constants from the real server ---------------------------------------------------------------

SERVER_ZONE: Final = ZoneInfo(os.environ.get("FAUXCALL_EVERYTHING_TZ", "America/New_York"))
"""The zone of the real server's ``toLocaleTimeString()`` (observed: US Eastern)."""

INTERNAL_ERROR: Final = -32603
INVALID_PARAMS: Final = -32602
REQUEST_TIMEOUT_TEXT: Final = "MCP error -32001: Request timed out"
SDK_REQUEST_TIMEOUT_S: Final = 60.0
ELICITATION_TIMEOUT_S: Final = 600.0
SIMULATION_PACE_S: Final = 5.0
MAX_TIMER_MS: Final = 2_147_483_647

TEXT_URI: Final = "demo://resource/dynamic/text"
BLOB_URI: Final = "demo://resource/dynamic/blob"
SESSION_URI: Final = "demo://resource/session"
_DYNAMIC: Final = re.compile(r"demo://resource/dynamic/(text|blob)/([^/,]+)")
"""The SDK's ``UriTemplate`` match: the variable is taken as is (never percent-decoded)."""
DEFAULT_GZIP_SOURCE: Final = (
    "https://raw.githubusercontent.com/modelcontextprotocol/servers/refs/heads/main/README.md"
)
GZIP_HEADER: Final = bytes.fromhex("1f8b0800000000000013")
"""Node's ``gzipSync`` header: no name, no mtime, OS byte 0x13 (as recorded)."""

LOG_LEVELS: Final = (
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
)
LOG_TEXT: Final = {
    "debug": "Debug-level message",
    "info": "Info-level message",
    "notice": "Notice-level message",
    "warning": "Warning-level message",
    "error": "Error-level message",
    "critical": "Critical-level message",
    "alert": "Alert level-message",  # sic: the real server's spelling
    "emergency": "Emergency-level message",
}

WEATHER: Final[dict[str, JsonObject]] = {
    "New York": {"temperature": 33, "conditions": "Cloudy", "humidity": 82},
    "Chicago": {"temperature": 36, "conditions": "Light rain / drizzle", "humidity": 82},
    "Los Angeles": {"temperature": 73, "conditions": "Sunny / Clear", "humidity": 48},
}

ANNOTATED: Final[dict[str, JsonObject]] = {
    "error": {
        "type": "text",
        "text": "Error: Operation failed",
        "annotations": {"audience": ["user", "assistant"], "priority": 1},
    },
    "success": {
        "type": "text",
        "text": "Operation completed successfully",
        "annotations": {"audience": ["user"], "priority": 0.7},
    },
    "debug": {
        "type": "text",
        "text": "Debug: Cache hit ratio 0.95, latency 150ms",
        "annotations": {"audience": ["assistant"], "priority": 0.3},
    },
}

TEAMS: Final = {
    "Engineering": ("Alice", "Bob", "Charlie"),
    "Sales": ("David", "Eve", "Frank"),
    "Marketing": ("Grace", "Henry", "Iris"),
    "Support": ("John", "Kim", "Lee"),
}
RESOURCE_TYPES: Final = ("Text", "Blob")

STAGES: Final = (
    "Gathering sources",
    "Analyzing content",
    "Synthesizing findings",
    "Generating report",
)
STAGE_MS: Final = 1002
"""A research stage lasts 1 s plus the store's bookkeeping (recorded: about 1002 ms apart)."""
TASK_TTL_MS: Final = 300_000
MAX_COMPLETIONS: Final = 100
TASK_POLL_MS: Final = 1000
RELATED_TASK: Final = "io.modelcontextprotocol/related-task"
NO_ELICITATION: Final = "technical (default - elicitation unavailable)"


def _asset(name: str) -> bytes:
    return resources.files("fauxcall_everything").joinpath("assets", name).read_bytes()


LOGO_PNG_BASE64: Final = _asset("mcp-logo.png.b64").decode().strip()
DEFAULT_README: Final = _asset("default-readme.md")


# -- per-session state ---------------------------------------------------------------------------


@dataclass
class ResearchTask:
    """One ``simulate-research-query`` task, its timeline computed from the fake clock."""

    task_id: str
    topic: str
    created_ms: int
    events: list[tuple[int, str, str]]
    """``(offset ms, status, statusMessage)``; the last one is the completion."""
    clarification: str | None = None
    cancelled_ms: int | None = None
    done_ms: int | None = None
    """When ``tasks/result`` finished waiting for it: completed, whatever the clock says."""

    def state_at(self, now_ms: int) -> tuple[str, str, int]:
        """``(status, statusMessage, lastUpdatedAt ms)`` at a time."""
        if self.cancelled_ms is not None:
            return "cancelled", "Client cancelled task execution.", self.cancelled_ms
        status, message, updated = "working", f"{STAGES[0]}...", self.created_ms
        for offset, event_status, event_message in self.events:
            at = self.created_ms + offset
            if at > now_ms and self.done_ms is None:
                break
            status, message = event_status, event_message or message
            updated = at if self.done_ms is None else min(at, max(self.done_ms, self.created_ms))
        return status, message, updated

    @property
    def completes_ms(self) -> int:
        """When it completes."""
        return self.created_ms + self.events[-1][0]

    def describe(self, now_ms: int) -> JsonObject:
        """The task as ``tasks/get`` shows it."""
        status, message, updated = self.state_at(now_ms)
        return {
            "taskId": self.task_id,
            "status": status,
            "ttl": TASK_TTL_MS,
            "createdAt": _iso_ms(self.created_ms),
            "lastUpdatedAt": _iso_ms(updated),
            "pollInterval": TASK_POLL_MS,
            "statusMessage": message,
        }

    def expired(self, now_ms: int) -> bool:
        """The task store forgets a task its TTL after its last update."""
        return now_ms > self.state_at(now_ms)[2] + TASK_TTL_MS


@dataclass
class SessionState:
    """What one MCP session (one connected client) has done."""

    logging_task: asyncio.Task[None] | None = None
    logging_on: bool = False
    updates_task: asyncio.Task[None] | None = None
    updates_on: bool = False
    level: str | None = None
    subscriptions: list[str] = field(default_factory=list)
    resources: dict[str, tuple[str, str]] = field(default_factory=dict)
    """Session resource URI -> (name, base64 gzip), in registration order."""
    tasks: dict[str, ResearchTask] = field(default_factory=dict)
    url_elicitations: set[str] = field(default_factory=set)


def session_state(ctx: RequestContext) -> SessionState:
    """The calling MCP session's state."""
    sessions = cast("dict[str, SessionState]", ctx.state.setdefault("mcp_sessions", {}))
    return sessions.setdefault(ctx.mcp_session or "", SessionState())


# -- helpers -------------------------------------------------------------------------------------


def _now_ms(ctx: RequestContext) -> int:
    return ctx.now_ns // 1_000_000


def _iso_ms(epoch_ms: int) -> str:
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z"


def local_time(ctx: RequestContext) -> str:
    """``new Date().toLocaleTimeString()`` on the real server (en-US: ``6:17:52 PM``)."""
    moment = datetime.fromtimestamp(ctx.now_ns / 1e9, tz=SERVER_ZONE)
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment:%M:%S} {'AM' if moment.hour < 12 else 'PM'}"  # noqa: PLR2004


def text_result(text: str, *, error: bool = False) -> JsonObject:
    """A tool result with one text block."""
    result: JsonObject = {"content": [{"type": "text", "text": text}]}
    if error:
        result["isError"] = True
    return result


def _params(ctx: RequestContext) -> JsonObject:
    message = ctx.json_body if isinstance(ctx.json_body, dict) else {}
    params = message.get("params")
    return params if isinstance(params, dict) else {}


def check_task_param(ctx: RequestContext) -> JsonObject | None:
    """``params.task`` as the SDK checks it in any ``tools/call`` (``-32603`` when malformed)."""
    params = _params(ctx)
    if "task" not in params:
        return None
    task = params["task"]
    if not isinstance(task, dict):
        raise RpcError(
            INTERNAL_ERROR,
            zodlike.protocol_issues(
                [
                    {
                        "expected": "object",
                        "code": "invalid_type",
                        "path": ["params", "task"],
                        "message": "Invalid input: expected object, received "
                        + zodlike.received(task),
                    }
                ]
            ),
        )
    ttl = task.get("ttl", zodlike.MISSING)
    if ttl is not zodlike.MISSING and zodlike.received(ttl) != "number":
        raise RpcError(
            INTERNAL_ERROR,
            zodlike.protocol_issues(
                [
                    {
                        "expected": "number",
                        "code": "invalid_type",
                        "path": ["params", "task", "ttl"],
                        "message": "Invalid input: expected number, received "
                        + zodlike.received(ttl),
                    }
                ]
            ),
        )
    return task


def validated(ctx: RequestContext, arguments: JsonObject, fields: list[Field]) -> zodlike.Checked:
    """Arguments checked the way the SDK checks them before the tool runs."""
    check_task_param(ctx)
    return zodlike.check(arguments, fields)


def check_meta(ctx: RequestContext) -> None:
    """A ``progressToken`` that is neither a string nor a number: the transport refuses the message.

    Recorded: HTTP 400 ``-32700 Parse error: Invalid JSON-RPC message`` (``id`` null).
    """
    meta = _params(ctx).get("_meta")
    token = meta.get("progressToken", 0) if isinstance(meta, dict) else 0
    if zodlike.received(token) not in {"string", "number"}:
        body = {
            "jsonrpc": "2.0",
            "error": {"code": -32700, "message": "Parse error: Invalid JSON-RPC message"},
            "id": None,
        }
        raise EarlyResponse(JSONResponse(body, status_code=400))


def task_aware(handler: ToolHandler) -> ToolHandler:
    """The SDK runs a tool called with ``task`` and then refuses its (non-task) result.

    Recorded for echo, get-sum and an unknown tool alike: ``-32602 Invalid task creation
    result``, after the tool did whatever it does.
    """

    async def run(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        check_meta(ctx)
        task = check_task_param(ctx)
        result = await handler(ctx, arguments)
        if task is not None:
            raise RpcError(INVALID_PARAMS, TASK_CREATION_FAILED)
        return result

    run.__doc__ = handler.__doc__
    return run


def _resource_text(ctx: RequestContext, uri: str, resource_id: float) -> JsonObject:
    stamp = local_time(ctx)
    return {
        "uri": uri,
        "mimeType": "text/plain",
        "text": f"Resource {js_string(resource_id)}: This is a plaintext resource created at "
        + stamp,
    }


def _resource_blob(ctx: RequestContext, uri: str, resource_id: float) -> JsonObject:
    stamp = local_time(ctx)
    text = f"Resource {js_string(resource_id)}: This is a base64 blob created at {stamp}"
    return {"uri": uri, "mimeType": "text/plain", "blob": base64.b64encode(text.encode()).decode()}


def _log(state: SessionState, channel: Channel | RequestContext, level: str, data: str) -> None:
    """``server.sendLoggingMessage``: sent unless below the session's chosen level."""
    if state.level is not None and LOG_LEVELS.index(level) < LOG_LEVELS.index(state.level):
        return
    channel.push("notifications/message", {"level": level, "data": data})


def _resource_list_changed(ctx: RequestContext) -> None:
    ctx.push("notifications/resources/list_changed")


async def _never_answered(ctx: RequestContext, timeout_s: float) -> JsonObject:
    """A request to the client that the fake can't send: the SDK gives up after its timeout."""
    await ctx.pause(timeout_s)
    return text_result(REQUEST_TIMEOUT_TEXT, error=True)


# -- simple tools ----------------------------------------------------------------------------------


async def echo(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``echo``: ``Echo: <message>``."""
    checked = validated(ctx, arguments, [Field("message", "string")])
    if not checked.ok:
        return text_result(zodlike.tool_error_text("echo", checked), error=True)
    return text_result(f"Echo: {checked.values['message']}")


async def get_annotated_message(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-annotated-message``: an annotated text block, optionally an annotated image."""
    name = "get-annotated-message"
    checked = validated(
        ctx,
        arguments,
        [
            Field("messageType", "enum", options=("error", "success", "debug")),
            Field("includeImage", "boolean", default=False),
        ],
    )
    if not checked.ok:
        return text_result(zodlike.tool_error_text(name, checked), error=True)
    content: list[JsonValue] = [dict(ANNOTATED[str(checked.values["messageType"])])]
    if checked.values["includeImage"] is True:
        content.append(
            {
                "type": "image",
                "data": LOGO_PNG_BASE64,
                "mimeType": "image/png",
                "annotations": {"audience": ["user"], "priority": 0.5},
            }
        )
    return {"content": content}


async def get_resource_links(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-resource-links``: alternating blob/text links for ids 1..count."""
    name = "get-resource-links"
    checked = validated(
        ctx, arguments, [Field("count", "number", default=3, minimum=1, maximum=10)]
    )
    if not checked.ok:
        return text_result(zodlike.tool_error_text(name, checked), error=True)
    count = as_double(cast("float", checked.values["count"]))
    content: list[JsonValue] = [
        {
            "type": "text",
            "text": f"Here are {js_string(count)} resource links to resources available in "
            "this server:",
        }
    ]
    resource_id = 1
    while resource_id <= count:
        kind = "Text" if resource_id % 2 == 0 else "Blob"
        base = TEXT_URI if kind == "Text" else BLOB_URI
        content.append(
            {
                "name": f"{kind} Resource {resource_id}",
                "uri": f"{base}/{resource_id}",
                "description": f"Resource {resource_id}: plaintext resource",
                "mimeType": "text/plain",
                "type": "resource_link",
            }
        )
        resource_id += 1
    return {"content": content}


async def get_resource_reference(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-resource-reference``: an embedded dynamic resource between two text blocks."""
    name = "get-resource-reference"
    checked = validated(
        ctx,
        arguments,
        [
            Field("resourceType", "enum", default="Text", options=RESOURCE_TYPES),
            Field("resourceId", "number", default=1),
        ],
    )
    if not checked.ok:
        return text_result(zodlike.tool_error_text(name, checked), error=True)
    resource_id = as_double(cast("float", checked.values["resourceId"]))
    if not is_positive_integer(resource_id):
        return text_result(
            f"Invalid resourceId: {js_string(resource_id)}. Must be a finite positive integer.",
            error=True,
        )
    shown = js_string(resource_id)
    if checked.values["resourceType"] == "Text":
        resource = _resource_text(ctx, f"{TEXT_URI}/{shown}", resource_id)
    else:
        resource = _resource_blob(ctx, f"{BLOB_URI}/{shown}", resource_id)
    return {
        "content": [
            {"type": "text", "text": f"Returning resource reference for Resource {shown}:"},
            {"type": "resource", "resource": resource},
            {
                "type": "text",
                "text": f"You can access this resource using the URI: {resource['uri']}",
            },
        ]
    }


async def get_structured_content(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-structured-content``: fixed weather per city, as text and ``structuredContent``."""
    name = "get-structured-content"
    checked = validated(ctx, arguments, [Field("location", "enum", options=tuple(WEATHER))])
    if not checked.ok:
        return text_result(zodlike.tool_error_text(name, checked), error=True)
    weather = WEATHER[str(checked.values["location"])]
    return {
        "content": [{"type": "text", "text": json.dumps(weather, separators=(",", ":"))}],
        "structuredContent": dict(weather),
    }


async def get_sum(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-sum``: ``The sum of a and b is a+b.`` in JavaScript arithmetic and formatting."""
    checked = validated(ctx, arguments, [Field("a", "number"), Field("b", "number")])
    if not checked.ok:
        return text_result(zodlike.tool_error_text("get-sum", checked), error=True)
    a = as_double(cast("float", checked.values["a"]))
    b = as_double(cast("float", checked.values["b"]))
    return text_result(f"The sum of {js_string(a)} and {js_string(b)} is {js_string(a + b)}.")


async def trigger_long_running_operation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-long-running-operation``: ``steps`` waits over ``duration`` seconds."""
    name = "trigger-long-running-operation"
    checked = validated(
        ctx,
        arguments,
        [Field("duration", "number", default=10), Field("steps", "number", default=5)],
    )
    if not checked.ok:
        return text_result(zodlike.tool_error_text(name, checked), error=True)
    duration = as_double(cast("float", checked.values["duration"]))
    steps = as_double(cast("float", checked.values["steps"]))
    step_ms = duration / steps * 1000 if steps else float("inf")
    meta = _params(ctx).get("_meta")
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    step = 1
    while step < steps + 1:
        await ctx.pause(_timer_seconds(step_ms))
        if token is not None:
            ctx.notify(
                "notifications/progress",
                {"progress": step, "total": _plain(steps), "progressToken": token},
            )
        step += 1
    return text_result(
        f"Long running operation completed. Duration: {js_string(duration)} seconds, "
        f"Steps: {js_string(steps)}."
    )


def _timer_seconds(delay_ms: float) -> float:
    """Node's ``setTimeout``: negative, NaN or beyond 2**31-1 ms delays fire after 1 ms."""
    if delay_ms != delay_ms or delay_ms > MAX_TIMER_MS or delay_ms < 1:  # noqa: PLR0124 - NaN
        return 0.001
    return delay_ms / 1000


def _plain(number: float) -> JsonValue:
    """A JSON number the way JavaScript serializes it (``2`` rather than ``2.0``)."""
    return int(number) if number.is_integer() and abs(number) < 2**53 else number


# -- session simulations -------------------------------------------------------------------------


async def toggle_simulated_logging(ctx: RequestContext, arguments: JsonObject) -> JsonValue:  # noqa: ARG001
    """``toggle-simulated-logging``: start or stop a random-level log message every 5 s."""
    check_task_param(ctx)
    state = session_state(ctx)
    session = ctx.mcp_session
    if state.logging_on:
        if state.logging_task is not None:
            state.logging_task.cancel()
        state.logging_on, state.logging_task = False, None
        return text_result(f"Stopped simulated logging for session {session}")
    suffix = f" - SessionId {session}" if session else ""
    rng = ctx.rng

    def send(channel: Channel | RequestContext) -> None:
        level = LOG_LEVELS[rng.randrange(len(LOG_LEVELS))]
        _log(state, channel, level, f"{LOG_TEXT[level]}{suffix}")

    async def every_five_seconds(channel: Channel) -> None:
        while True:
            await channel.pause(SIMULATION_PACE_S)
            send(channel)

    send(ctx)  # once at once, then on a 5 second pace
    state.logging_task = ctx.background(every_five_seconds)
    state.logging_on = True
    return text_result(
        f"Started simulated, random-leveled logging for session {session} at a 5 second pace. "
        "Client's selected logging level will be respected. If an interval elapses and the "
        "message to be sent is below the selected level, it will not be sent. Thus at higher "
        "chosen logging levels, messages should arrive further apart. "
    )


async def toggle_subscriber_updates(ctx: RequestContext, arguments: JsonObject) -> JsonValue:  # noqa: ARG001
    """``toggle-subscriber-updates``: start or stop ``resources/updated`` for subscriptions."""
    check_task_param(ctx)
    state = session_state(ctx)
    session = ctx.mcp_session
    if state.updates_on:
        if state.updates_task is not None:
            state.updates_task.cancel()
        state.updates_on, state.updates_task = False, None
        return text_result(f"Stopped simulated resource updates for session {session}")

    def send(channel: Channel | RequestContext) -> None:
        for uri in list(state.subscriptions):
            channel.push("notifications/resources/updated", {"uri": uri})

    async def every_five_seconds(channel: Channel) -> None:
        while True:
            await channel.pause(SIMULATION_PACE_S)
            send(channel)

    send(ctx)
    state.updates_task = ctx.background(every_five_seconds)
    state.updates_on = True
    return text_result(
        f"Started simulated resource updated notifications for session {session} at a 5 second "
        "pace. Client will receive updates for any resources the it is subscribed to."
    )


# -- gzip-file-as-resource ------------------------------------------------------------------------


class FetchFailedError(Exception):
    """What Node's ``fetch`` reports for a URL it can't load: ``fetch failed``."""


def gzip_like_node(data: bytes) -> bytes:
    """``zlib.gzipSync(data)``: byte-identical to the recorded output."""
    compressor = zlib.compressobj(6, zlib.DEFLATED, -15, 8)
    body = compressor.compress(data) + compressor.flush()
    trailer = struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)
    return GZIP_HEADER + body + trailer


def fetch(url: str) -> bytes:
    """The bytes ``fetch(url)`` returns: data URLs decoded, the default README replayed.

    The fake never goes to the network: any other http(s) URL fails like an unreachable host.
    """
    if url.startswith("data:"):
        return decode_data_url(url)
    if url == DEFAULT_GZIP_SOURCE:
        return DEFAULT_README
    raise FetchFailedError


def decode_data_url(url: str) -> bytes:
    """WHATWG data: URL processing (percent-decoding, forgiving base64)."""
    header, comma, body = url[len("data:") :].partition(",")
    if not comma:
        raise FetchFailedError
    data = unquote_to_bytes(body)
    if re.search(r";\s*base64\s*$", header, flags=re.IGNORECASE) is None:
        return data
    compact = re.sub(rb"[\t\n\f\r ]", b"", data)
    if len(compact) % 4 == 0 and compact.endswith(b"=="):
        compact = compact[:-2]
    elif len(compact) % 4 == 0 and compact.endswith(b"="):
        compact = compact[:-1]
    if len(compact) % 4 == 1 or re.fullmatch(rb"[A-Za-z0-9+/]*", compact) is None:
        raise FetchFailedError
    try:
        return base64.b64decode(compact + b"=" * (-len(compact) % 4), validate=True)
    except binascii.Error as exc:  # pragma: no cover - the checks above catch these
        raise FetchFailedError from exc


URL_TRIM: Final = "".join(map(chr, range(0x21)))
"""What WHATWG URL parsing strips from both ends (C0 controls and space)."""
_URL: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):(.*)", re.DOTALL)
_SPECIAL: Final = frozenset({"http", "https", "ws", "wss", "ftp", "file"})
_PATH_ESCAPES: Final = frozenset(' "#<>?`{}')
_QUERY_ESCAPES: Final = frozenset(' "#<>')
_FRAGMENT_ESCAPES: Final = frozenset(' "<>`')


def _escape(text: str, escapes: frozenset[str]) -> str:
    out: list[str] = []
    for char in text:
        if char in escapes or ord(char) < 0x20 or ord(char) > 0x7E:  # noqa: PLR2004
            out.extend(f"%{byte:02X}" for byte in char.encode())
        else:
            out.append(char)
    return "".join(out)


def _without_dot_segments(path: str) -> str:
    kept: list[str] = []
    segments = path.split("/")[1:]
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "..":
            if kept:
                kept.pop()
            if last:
                kept.append("")
        elif segment == ".":
            if last:
                kept.append("")
        else:
            kept.append(segment)
    return "/" + "/".join(kept)


def normalize_uri(uri: str) -> str | None:
    """``new URL(uri).href``: ``None`` when it throws (the real server: ``Invalid URL``).

    Scheme lowercased, dot segments removed, characters escaped as WHATWG does for each part.
    """
    text = re.sub(r"[\t\n\r]", "", uri.strip(URL_TRIM))
    match = _URL.fullmatch(text)
    if match is None:
        return None
    scheme, rest = match.group(1).lower(), match.group(2)
    rest, has_fragment, fragment = rest.partition("#")
    rest, has_query, query = rest.partition("?")
    if rest.startswith("//"):
        authority, slash, path = rest[2:].partition("/")
        if scheme in _SPECIAL:
            authority = authority.lower()
            if not authority and scheme != "file":
                return None
        head = f"//{authority}"
        path = _without_dot_segments(slash + path) if slash else ""
    else:
        head, path = "", rest
    href = f"{scheme}:{head}{_escape(path, _PATH_ESCAPES)}"
    if has_query:
        href += "?" + _escape(query, _QUERY_ESCAPES)
    if has_fragment:
        href += "#" + _escape(fragment, _FRAGMENT_ESCAPES)
    return href


async def gzip_file_as_resource(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``gzip-file-as-resource``: compress a URL's content into a session resource."""
    name = "gzip-file-as-resource"
    checked = validated(
        ctx,
        arguments,
        [
            Field("name", "string", default="README.md.gz"),
            Field("data", "url", default=DEFAULT_GZIP_SOURCE),
            Field(
                "outputType", "enum", default="resourceLink", options=("resourceLink", "resource")
            ),
        ],
    )
    if not checked.ok:
        return text_result(zodlike.tool_error_text(name, checked), error=True)
    file_name = str(checked.values["name"])
    given = str(checked.values["data"])
    source = given.strip(URL_TRIM)
    scheme = source.split(":", 1)[0].lower()
    if scheme not in {"http", "https", "data"}:
        return text_result(
            f"Error processing file {given}: Unsupported URL protocol for {given}. "
            "Only http, https, and data URLs are supported.",
            error=True,
        )
    try:
        content = fetch(source)
    except FetchFailedError:
        return text_result("fetch failed", error=True)
    blob = base64.b64encode(gzip_like_node(content)).decode()
    uri = f"{SESSION_URI}/{file_name}"
    state = session_state(ctx)
    if state.resources.pop(uri, None) is not None:
        _resource_list_changed(ctx)  # the old registration is removed first
    state.resources[uri] = (file_name, blob)
    _resource_list_changed(ctx)
    if checked.values["outputType"] == "resource":
        return {
            "content": [
                {
                    "type": "resource",
                    "resource": {"uri": uri, "mimeType": "application/gzip", "blob": blob},
                }
            ]
        }
    return {
        "content": [
            {"name": file_name, "uri": uri, "mimeType": "application/gzip", "type": "resource_link"}
        ]
    }


# -- tools that send requests to the client -------------------------------------------------------


async def trigger_sampling_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-sampling-request``: the fake can't ask the client to sample; it times out."""
    name = "trigger-sampling-request"
    checked = validated(
        ctx,
        arguments,
        [Field("prompt", "string"), Field("maxTokens", "number", default=100)],
    )
    if not checked.ok:
        return text_result(zodlike.tool_error_text(name, checked), error=True)
    return await _never_answered(ctx, SDK_REQUEST_TIMEOUT_S)


async def trigger_sampling_request_async(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-sampling-request-async``: like the synchronous one, it times out."""
    name = "trigger-sampling-request-async"
    checked = validated(
        ctx,
        arguments,
        [Field("prompt", "string"), Field("maxTokens", "number", default=100)],
    )
    if not checked.ok:
        return text_result(zodlike.tool_error_text(name, checked), error=True)
    return await _never_answered(ctx, SDK_REQUEST_TIMEOUT_S)


async def trigger_elicitation_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:  # noqa: ARG001
    """``trigger-elicitation-request``: waits its 10 minutes for an answer that never comes."""
    check_task_param(ctx)
    return await _never_answered(ctx, ELICITATION_TIMEOUT_S)


async def trigger_elicitation_request_async(
    ctx: RequestContext,
    arguments: JsonObject,  # noqa: ARG001
) -> JsonValue:
    """``trigger-elicitation-request-async``: the elicitation task request times out."""
    check_task_param(ctx)
    return await _never_answered(ctx, SDK_REQUEST_TIMEOUT_S)


async def trigger_url_elicitation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-url-elicitation``: the error path (``-32042``); the request path times out."""
    name = "trigger-url-elicitation"
    checked = validated(
        ctx,
        arguments,
        [
            Field("url", "url"),
            Field("message", "string", default="Please open the link to complete this action."),
            Field("elicitationId", "string", optional=True),
            Field("errorPath", "boolean", default=False),
        ],
    )
    if not checked.ok:
        return text_result(zodlike.tool_error_text(name, checked), error=True)
    state = session_state(ctx)
    requested_id = checked.values.get("elicitationId")
    key = f"{checked.values['url']}\x00{requested_id or ''}"
    if checked.values["errorPath"] is True:
        if key in state.url_elicitations:
            state.url_elicitations.discard(key)
        else:
            state.url_elicitations.add(key)
            raise RpcError(
                -32042,
                "MCP error -32042: This request requires browser-based authorization.",
                {
                    "elicitations": [
                        {
                            "mode": "url",
                            "url": "https://modelcontextprotocol.io",
                            "message": "Open this link to satisfy the prerequisite, then retry "
                            "the request.",
                            "elicitationId": _uuid4(ctx),
                        }
                    ]
                },
            )
    return await _never_answered(ctx, ELICITATION_TIMEOUT_S)


def _uuid4(ctx: RequestContext) -> str:
    raw = bytearray(ctx.rng.getrandbits(8) for _ in range(16))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    text = raw.hex()
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


async def get_roots_list(ctx: RequestContext, arguments: JsonObject) -> JsonValue:  # noqa: ARG001
    """``get-roots-list``: the server's ``roots/list`` request goes unanswered, so no roots."""
    check_task_param(ctx)
    await ctx.pause(SDK_REQUEST_TIMEOUT_S)
    return text_result(
        "The client supports roots but no roots are currently configured.\n\n"
        "This could mean:\n"
        "1. The client hasn't provided any roots yet\n"
        "2. The client provided an empty roots list\n"
        "3. The roots configuration is still being loaded"
    )


# -- simulate-research-query and tasks ------------------------------------------------------------


def _research_events(ambiguous: bool, topic: str) -> tuple[list[tuple[int, str, str]], str | None]:  # noqa: FBT001
    """The task's timeline: a stage a second, and the unanswered clarification when asked."""
    events: list[tuple[int, str, str]] = []
    offset = 0
    clarification: str | None = None
    for index, stage in enumerate(STAGES):
        events.append((offset, "working", f"{stage}..."))
        if index == 2 and ambiguous:  # noqa: PLR2004 - the real server asks at the third stage
            events.append(
                (
                    offset,
                    "input_required",
                    f'Found multiple interpretations for "{topic}". Requesting clarification...',
                )
            )
            offset += int(SDK_REQUEST_TIMEOUT_S * 1000)
            clarification = NO_ELICITATION
            events.append(
                (offset, "working", f'Continuing with interpretation: "{clarification}"...')
            )
        offset += STAGE_MS
    events.append((offset, "completed", ""))
    return events, clarification


def research_report(topic: str, clarification: str | None) -> str:
    """The report ``tasks/result`` returns (the real server's template)."""
    title = f"{topic} ({clarification})" if clarification else topic
    stages = "\n".join(f"- Stage {i + 1}: {stage} ✓" for i, stage in enumerate(STAGES))
    progressed = "`input_required` → `working` → " if clarification else ""
    elicitation = (
        "**Elicitation Flow:**\n"
        "When the query was ambiguous, the server sent an `elicitation/create` request\n"
        "to the client. The task status changed to `input_required` while awaiting user input.\n"
        "**Note:** Elicitation failed and a default interpretation was used.\n"
        if clarification
        else ""
    )
    return (
        f"# Research Report: {title}\n\n"
        "## Research Parameters\n"
        f"- **Topic**: {topic}\n"
        f"{f'- **Clarification**: {clarification}' if clarification else ''}\n\n"
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
        f"3. Status progressed: `working` → {progressed}`completed`\n"
        "4. Client calls `tasks/result` → Server returns this final result\n\n"
        f"{elicitation}\n"
        "**Key Concepts:**\n"
        '- Tasks enable "call now, fetch later" patterns\n'
        "- `statusMessage` provides human-readable progress updates\n"
        "- Tasks have TTL (time-to-live) for automatic cleanup\n"
        "- `pollInterval` suggests how often to check status\n"
        "- Elicitation requests use `relatedTask` to queue via tasks/result (works on all "
        "transports)\n\n"
        "*This is a simulated research report from the Everything MCP Server.*\n"
    )


TASK_CREATION_FAILED: Final = "MCP error -32602: Invalid task creation result: " + (
    zodlike.protocol_issues(
        [
            {
                "expected": "object",
                "code": "invalid_type",
                "path": ["task"],
                "message": "Invalid input: expected object, received undefined",
            }
        ]
    )
)


async def simulate_research_query(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``simulate-research-query``: a task (required) that goes through four stages."""
    check_meta(ctx)
    task = check_task_param(ctx)
    if task is None:
        return text_result(
            "MCP error -32601: Tool simulate-research-query requires task augmentation "
            "(taskSupport: 'required')",
            error=True,
        )
    checked = zodlike.check(
        arguments, [Field("topic", "string"), Field("ambiguous", "boolean", default=False)]
    )
    if not checked.ok:  # the SDK reports the failed handler as a malformed creation result
        raise RpcError(INVALID_PARAMS, TASK_CREATION_FAILED)
    topic = str(checked.values["topic"])
    asks = checked.values["ambiguous"] is True and "elicitation" in ctx.client_capabilities
    events, clarification = _research_events(asks, topic)
    research = ResearchTask(
        task_id=secrets.token_hex(16),
        topic=topic,
        created_ms=_now_ms(ctx),
        events=events,
        clarification=clarification,
    )
    session_state(ctx).tasks[research.task_id] = research
    return {"task": research.describe(research.created_ms)}


def _task_id(params: JsonObject) -> str:
    task_id = params.get("taskId", zodlike.MISSING)
    if not isinstance(task_id, str):
        raise RpcError(INTERNAL_ERROR, zodlike.missing_string_param("taskId", task_id))
    return task_id


def _live_task(ctx: RequestContext, task_id: str) -> ResearchTask | None:
    tasks = session_state(ctx).tasks
    task = tasks.get(task_id)
    if task is not None and task.expired(_now_ms(ctx)):
        del tasks[task_id]
        return None
    return task


async def tasks_get(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/get``: the task's state now."""
    task = _live_task(ctx, _task_id(params))
    if task is None:
        raise RpcError(INVALID_PARAMS, "MCP error -32602: Failed to retrieve task: Task not found")
    return task.describe(_now_ms(ctx))


async def tasks_result(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/result``: waits for the task to finish, then its report."""
    task_id = _task_id(params)
    task = _live_task(ctx, task_id)
    if task is None:
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Task not found: {task_id}")
    if task.cancelled_ms is not None:
        raise RpcError(INTERNAL_ERROR, f"Task {task_id} has no result stored")
    remaining_ms = task.completes_ms - _now_ms(ctx)
    if remaining_ms > 0 and task.done_ms is None:
        await ctx.pause(remaining_ms / 1000)
    if task.done_ms is None:
        task.done_ms = max(_now_ms(ctx), min(task.completes_ms, _now_ms(ctx) + remaining_ms))
    result: JsonObject = text_result(research_report(task.topic, task.clarification))
    result["_meta"] = {RELATED_TASK: {"taskId": task_id}}
    return result


async def tasks_cancel(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/cancel``: cancels a working task; a finished one can't be."""
    task_id = _task_id(params)
    task = _live_task(ctx, task_id)
    if task is None:
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Task not found: {task_id}")
    now = _now_ms(ctx)
    status = task.state_at(now)[0]
    if status in {"completed", "cancelled", "failed"}:
        raise RpcError(
            INVALID_PARAMS, f"MCP error -32602: Cannot cancel task in terminal status: {status}"
        )
    task.cancelled_ms = now
    return {"_meta": {}, **task.describe(now)}


TASKS_PAGE: Final = 10
"""The SDK task store's page size; ``nextCursor`` is the page's last task id (recorded)."""


async def tasks_list(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/list``: this session's tasks, oldest first, ten a page."""
    cursor = params.get("cursor", zodlike.MISSING)
    if cursor is not zodlike.MISSING and not isinstance(cursor, str):
        raise RpcError(INTERNAL_ERROR, zodlike.missing_string_param("cursor", cursor))
    now = _now_ms(ctx)
    tasks = session_state(ctx).tasks
    for task_id in [i for i, task in tasks.items() if task.expired(now)]:
        del tasks[task_id]
    ids = list(tasks)
    start = 0
    if isinstance(cursor, str):
        if cursor not in tasks:
            raise RpcError(
                INVALID_PARAMS, f"MCP error -32602: Failed to list tasks: Invalid cursor: {cursor}"
            )
        start = ids.index(cursor) + 1
    page = ids[start : start + TASKS_PAGE]
    body: JsonObject = {"tasks": [tasks[i].describe(now) for i in page]}
    if start + TASKS_PAGE < len(ids):
        body["nextCursor"] = page[-1]
    body["_meta"] = {}
    return body


# -- resources -----------------------------------------------------------------------------------


def _static_uris(ctx: RequestContext) -> list[str]:
    mcp = ctx.model.mcp
    listed = mcp.resources if mcp is not None else []
    return [str(r["uri"]) for r in listed if isinstance(r, dict) and "uri" in r]


def recorded_document(ctx: RequestContext, uri: str) -> JsonValue:
    """A static document's ``resources/read`` result, as recorded (the files never change)."""
    for exchange in reversed(ctx.runtime.recordings.for_endpoint("MCP resources/read")):
        sent = exchange.request.body.parsed()
        params = sent.get("params") if isinstance(sent, dict) else None
        reply = exchange.response.body.parsed() if exchange.response is not None else None
        if (
            isinstance(params, dict)
            and params.get("uri") == uri
            and isinstance(reply, dict)
            and "result" in reply
        ):
            return reply["result"]
    return None


async def resources_read(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/read``: dynamic and session resources computed, static documents replayed."""
    raw = params.get("uri", zodlike.MISSING)
    if not isinstance(raw, str):
        raise RpcError(INTERNAL_ERROR, zodlike.missing_string_param("uri", raw))
    uri = normalize_uri(raw)
    if uri is None:
        raise RpcError(INTERNAL_ERROR, "Invalid URL")
    session = session_state(ctx).resources
    if uri in session:
        file_name, blob = session[uri]
        del file_name
        return {"contents": [{"uri": uri, "mimeType": "application/gzip", "blob": blob}]}
    if uri in _static_uris(ctx):
        document = recorded_document(ctx, uri)
        if document is not None:
            return document
    dynamic = _DYNAMIC.fullmatch(uri)
    if dynamic is not None:
        resource_id = js_number(dynamic.group(2))
        if not is_positive_integer(resource_id):
            raise RpcError(INTERNAL_ERROR, f"Unknown resource: {uri}")
        make = _resource_text if dynamic.group(1) == "text" else _resource_blob
        return {"contents": [make(ctx, uri, resource_id)]}
    raise RpcError(INVALID_PARAMS, f"MCP error -32602: Resource {uri} not found")


async def resources_list(ctx: RequestContext, params: JsonObject) -> JsonValue:  # noqa: ARG001
    """``resources/list``: the static documents, then this session's resources."""
    mcp = ctx.model.mcp
    listed: list[JsonValue] = list(mcp.resources) if mcp is not None else []
    for uri, (file_name, _) in session_state(ctx).resources.items():
        listed.append({"uri": uri, "name": file_name, "mimeType": "application/gzip"})
    return {"resources": listed}


async def resources_subscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/subscribe``: remembered for ``toggle-subscriber-updates`` (any URI)."""
    uri = params.get("uri", zodlike.MISSING)
    if not isinstance(uri, str):
        raise RpcError(INTERNAL_ERROR, zodlike.missing_string_param("uri", uri))
    state = session_state(ctx)
    _log(state, ctx, "info", f"Received Subscribe Resource request for URI: {uri} {_from(ctx)}")
    if uri not in state.subscriptions:
        state.subscriptions.append(uri)
    return {}


async def resources_unsubscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/unsubscribe``: forgotten (unknown URIs are fine)."""
    uri = params.get("uri", zodlike.MISSING)
    if not isinstance(uri, str):
        raise RpcError(INTERNAL_ERROR, zodlike.missing_string_param("uri", uri))
    state = session_state(ctx)
    _log(state, ctx, "info", f"Received Unsubscribe Resource request: {uri} {_from(ctx)}")
    if uri in state.subscriptions:
        state.subscriptions.remove(uri)
    return {}


def _from(ctx: RequestContext) -> str:
    return f"from session {ctx.mcp_session}" if ctx.mcp_session else ""


async def logging_set_level(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``logging/setLevel``: the minimum level of this session's log messages."""
    level = params.get("level", zodlike.MISSING)
    if not isinstance(level, str) or level not in LOG_LEVELS:
        choices = "|".join(f'"{name}"' for name in LOG_LEVELS)
        raise RpcError(
            INTERNAL_ERROR,
            zodlike.protocol_issues(
                [
                    {
                        "code": "invalid_value",
                        "values": list(LOG_LEVELS),
                        "path": ["params", "level"],
                        "message": f"Invalid option: expected one of {choices}",
                    }
                ]
            ),
        )
    session_state(ctx).level = level
    return {}


# -- prompts and completions ---------------------------------------------------------------------


def _prompt_args(name: str, arguments: JsonObject, fields: list[Field]) -> dict[str, JsonValue]:
    checked = zodlike.check(arguments, fields)
    if not checked.ok:
        raise RpcError(INVALID_PARAMS, zodlike.prompt_error_message(name, checked))
    return checked.values


def _user_text(text: str) -> JsonObject:
    return {"role": "user", "content": {"type": "text", "text": text}}


async def prompts_get(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``prompts/get`` for the four prompts."""
    name = params.get("name")
    raw = params.get("arguments", zodlike.MISSING)
    issues = _string_record_issues(raw, ["params", "arguments"])
    if not isinstance(name, str):
        issues.insert(0, _string_issue(params.get("name", zodlike.MISSING), ["params", "name"]))
    if issues:
        raise RpcError(INTERNAL_ERROR, zodlike.protocol_issues(issues))
    arguments: JsonObject = raw if isinstance(raw, dict) else {}
    match name:
        case "simple-prompt":
            return {"messages": [_user_text("This is a simple prompt without arguments.")]}
        case "args-prompt":
            values = _prompt_args(
                name,
                arguments,
                [Field("city", "string"), Field("state", "string", optional=True)],
            )
            state = values.get("state")
            where = f"{values['city']}{f', {state}' if state else ''}"
            return {"messages": [_user_text(f"What's weather in {where}?")]}
        case "completable-prompt":
            values = _prompt_args(
                name, arguments, [Field("department", "string"), Field("name", "string")]
            )
            text = (
                f"Please promote {values['name']} to the head of the {values['department']} team."
            )
            return {"messages": [_user_text(text)]}
        case "resource-prompt":
            return resource_prompt(ctx, arguments)
        case _:
            raise RpcError(INVALID_PARAMS, f"MCP error -32602: Prompt {name} not found")


def _string_issue(value: JsonValue | object, path: list[JsonValue]) -> JsonObject:
    return {
        "expected": "string",
        "code": "invalid_type",
        "path": path,
        "message": f"Invalid input: expected string, received {zodlike.received(value)}",
    }


def _string_record_issues(value: JsonValue | object, path: list[JsonValue]) -> list[JsonObject]:
    """Zod issues of an optional ``z.record(z.string(), z.string())``."""
    if value is zodlike.MISSING:
        return []
    if not isinstance(value, dict):
        received = zodlike.received(value)
        return [
            {
                "expected": "record",
                "code": "invalid_type",
                "path": path,
                "message": f"Invalid input: expected record, received {received}",
            }
        ]
    return [
        _string_issue(item, [*path, key])
        for key, item in value.items()
        if not isinstance(item, str)
    ]


def resource_prompt(ctx: RequestContext, arguments: JsonObject) -> JsonObject:
    """``resource-prompt``: a dynamic resource embedded in the prompt."""
    values = _prompt_args(
        "resource-prompt",
        arguments,
        [Field("resourceType", "string"), Field("resourceId", "string")],
    )
    kind = str(values["resourceType"])
    if kind not in RESOURCE_TYPES:
        raise RpcError(INTERNAL_ERROR, f"Invalid resourceType: {kind}. Must be Text or Blob.")
    resource_id = js_number(str(values["resourceId"]))
    if not is_positive_integer(resource_id):
        raise RpcError(
            INTERNAL_ERROR,
            f"Invalid resourceId: {values['resourceId']}. Must be a finite positive integer.",
        )
    shown = js_string(resource_id)
    if kind == "Text":
        resource = _resource_text(ctx, f"{TEXT_URI}/{shown}", resource_id)
    else:
        resource = _resource_blob(ctx, f"{BLOB_URI}/{shown}", resource_id)
    intro = (
        f"This prompt includes the {kind} resource with id: {shown}. "
        "Please analyze the following resource:"
    )
    return {
        "messages": [
            _user_text(intro),
            {"role": "user", "content": {"type": "resource", "resource": resource}},
        ]
    }


def _completion(values: list[str] | None) -> JsonObject:
    """The SDK's completion result: no ``total`` when the argument has no completer."""
    if values is None:
        return {"completion": {"values": [], "hasMore": False}}
    return {
        "completion": {
            "values": cast("list[JsonValue]", values[:MAX_COMPLETIONS]),
            "total": len(values),
            "hasMore": len(values) > MAX_COMPLETIONS,
        }
    }


def _positive_id(value: str) -> list[str]:
    return [value] if is_positive_integer(js_number(value)) else []


def _prompt_completions(
    name: str, argument: str, value: str, context: JsonObject
) -> list[str] | None:
    if name == "completable-prompt":
        if argument == "department":
            return [d for d in TEAMS if d.startswith(value)]
        if argument == "name":
            department = context.get("department")
            team = TEAMS.get(department) if isinstance(department, str) else None
            return [n for n in team or () if n.startswith(value)]
    if name == "resource-prompt":
        if argument == "resourceType":
            return [t for t in RESOURCE_TYPES if t.startswith(value)]
        if argument == "resourceId":
            return _positive_id(value)
    return None


def _ref_union_issue(ref: JsonValue | object) -> JsonObject:
    """Zod's ``invalid_union`` issue for a ``ref`` that is neither kind of reference."""
    if not isinstance(ref, dict):
        return {
            "expected": "object",
            "code": "invalid_type",
            "path": ["params", "ref"],
            "message": f"Invalid input: expected object, received {zodlike.received(ref)}",
        }
    branches: list[JsonValue] = []
    for kind, key in (("ref/prompt", "name"), ("ref/resource", "uri")):
        issues: list[JsonValue] = [
            {
                "code": "invalid_value",
                "values": [kind],
                "path": ["type"],
                "message": f'Invalid input: expected "{kind}"',
            }
        ]
        value = ref.get(key, zodlike.MISSING)
        if not isinstance(value, str):
            issues.append(
                {
                    "expected": "string",
                    "code": "invalid_type",
                    "path": [key],
                    "message": "Invalid input: expected string, received "
                    + zodlike.received(value),
                }
            )
        branches.append(issues)
    return {
        "code": "invalid_union",
        "errors": branches,
        "path": ["params", "ref"],
        "message": "Invalid input",
    }


async def completion_complete(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``completion/complete`` for prompt arguments and resource template variables."""
    ref = params.get("ref", zodlike.MISSING)
    argument = params.get("argument", zodlike.MISSING)
    if not isinstance(ref, dict) or ref.get("type") not in {"ref/prompt", "ref/resource"}:
        raise RpcError(INTERNAL_ERROR, zodlike.protocol_issues([_ref_union_issue(ref)]))
    if not isinstance(argument, dict):
        raise RpcError(
            INTERNAL_ERROR,
            zodlike.protocol_issues(
                [
                    {
                        "expected": "object",
                        "code": "invalid_type",
                        "path": ["params", "argument"],
                        "message": "Invalid input: expected object, received "
                        + zodlike.received(argument),
                    }
                ]
            ),
        )
    issues = [
        _string_issue(argument.get(key, zodlike.MISSING), ["params", "argument", key])
        for key in ("name", "value")
        if not isinstance(argument.get(key), str)
    ]
    context = params.get("context")
    known = (
        context.get("arguments", zodlike.MISSING) if isinstance(context, dict) else zodlike.MISSING
    )
    issues += _string_record_issues(known, ["params", "context", "arguments"])
    if issues:
        raise RpcError(INTERNAL_ERROR, zodlike.protocol_issues(issues))
    arg_name = str(argument["name"])
    value = str(argument["value"])
    known_args: JsonObject = known if isinstance(known, dict) else {}
    match ref.get("type"), ref.get("name"), ref.get("uri"):
        case "ref/prompt", str() as prompt, _:
            if prompt not in {
                "simple-prompt",
                "args-prompt",
                "completable-prompt",
                "resource-prompt",
            }:
                raise RpcError(INVALID_PARAMS, f"MCP error -32602: Prompt {prompt} not found")
            return _completion(_prompt_completions(prompt, arg_name, value, known_args))
        case "ref/resource", _, str() as template:
            if template in _static_uris(ctx) or template in session_state(ctx).resources:
                return _completion(None)  # a fixed resource: nothing to complete
            if template not in {f"{TEXT_URI}/{{resourceId}}", f"{BLOB_URI}/{{resourceId}}"}:
                raise RpcError(
                    INVALID_PARAMS, f"MCP error -32602: Resource template {template} not found"
                )
            return _completion(_positive_id(value) if arg_name == "resourceId" else None)
        case _:
            return None


TOOL_HANDLERS.update(
    {
        "simulate-research-query": simulate_research_query,
    }
)
TOOL_HANDLERS.update(
    {
        name: task_aware(handler)
        for name, handler in {
            "echo": echo,
            "get-annotated-message": get_annotated_message,
            "get-resource-links": get_resource_links,
            "get-resource-reference": get_resource_reference,
            "get-structured-content": get_structured_content,
            "get-sum": get_sum,
            "trigger-long-running-operation": trigger_long_running_operation,
            "toggle-simulated-logging": toggle_simulated_logging,
            "toggle-subscriber-updates": toggle_subscriber_updates,
            "gzip-file-as-resource": gzip_file_as_resource,
            "trigger-sampling-request": trigger_sampling_request,
            "trigger-sampling-request-async": trigger_sampling_request_async,
            "trigger-elicitation-request": trigger_elicitation_request,
            "trigger-elicitation-request-async": trigger_elicitation_request_async,
            "trigger-url-elicitation": trigger_url_elicitation,
            "get-roots-list": get_roots_list,
        }.items()
    }
)
METHOD_HANDLERS.update(
    {
        "resources/read": resources_read,
        "resources/list": resources_list,
        "resources/subscribe": resources_subscribe,
        "resources/unsubscribe": resources_unsubscribe,
        "logging/setLevel": logging_set_level,
        "prompts/get": prompts_get,
        "completion/complete": completion_complete,
        "tasks/get": tasks_get,
        "tasks/result": tasks_result,
        "tasks/cancel": tasks_cancel,
        "tasks/list": tasks_list,
    }
)
