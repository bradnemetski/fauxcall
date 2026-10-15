"""The Everything reference server's behavior, tool by tool and method by method.

Every tool is ``computed``: its answers are functions of the arguments, the client (its
capabilities, its answers to the server's requests, its session id) and the clock, and the
recordings show exactly how. The texts and validation messages are the real server's, from
``@modelcontextprotocol/server-everything`` 2026.8.31 on SDK 1.32.1 and zod 4.6.6; where the
evidence came from the published source rather than a recording, the docstring says so.

Arguments are checked here (``invalid_args: accept`` in the model) so refusals read as zod's;
any tool called with a ``task`` runs, and then fails like the SDK's CreateTaskResult check.
"""

from __future__ import annotations

import base64
import math
import random
import re
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from fauxcall.runtime import ClientError, RpcError

from fauxcall_everything import constants, tasks
from fauxcall_everything.jsvalues import (
    is_integer,
    is_number,
    json_number,
    number_to_string,
    parse_number,
    stringify,
    template,
    to_double,
    truthy,
)
from fauxcall_everything.state import (
    LOG_LEVELS,
    ClientState,
    SessionResource,
    client_state,
    is_current,
    log,
    resource_owners,
    subscribers,
)
from fauxcall_everything.webdata import data_url_body, href, node_gzip, protocol
from fauxcall_everything.zodish import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    Field,
    client_task_reply,
    create_message_result,
    elicit_result,
    issues_text,
    parse_arguments,
    poll_reply,
    protocol_error,
    require_string,
    roots_result,
    tool_error,
    tool_input_error,
    type_issue,
    value_issue,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Response, WebSocket
    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.types import JsonObject, JsonValue

    ToolHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]
    MethodHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler (none: this fake is an MCP server)."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler (none)."""

TOOL_HANDLERS: dict[str, ToolHandler] = {}
"""MCP tool name -> its handler."""

METHOD_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP method -> its handler."""

SERVER_ZONE: Final = ZoneInfo("America/New_York")
"""The recorded server's local zone (its process had no TZ): 00:37 UTC printed as 8:37:06 PM."""
DEFAULT_GZIP_URL: Final = (
    "https://raw.githubusercontent.com/modelcontextprotocol/servers/refs/heads/main/README.md"
)
REMOTE_FILES: Final = {DEFAULT_GZIP_URL: "000210"}
"""Remote files the real server fetched while recording: URL -> the read of the resource it made."""
REMOTE_FETCH_SECONDS: Final = 0.27
"""How long the recorded fetch of the default file took (000209: 275 ms in all)."""
FAILED_FETCH_SECONDS: Final = 0.04
"""How long the recorded failed fetch took (000215: an unresolvable host, 44 ms in all)."""
SIMULATION_PERIOD: Final = 5.0
"""Simulated logging and resource updates repeat every 5 s (recordings 000253, 000274)."""
ROOTS_SYNC_DELAY: Final = 0.35
"""The server asks a roots-capable client for its roots 350 ms after it initialized (source)."""
SDK_REQUEST_TIMEOUT: Final = 60.0
"""The SDK's default timeout of a request the server sends (000372: 60 s without an answer)."""
ELICITATION_TIMEOUT: Final = 600.0
"""The elicitation tools wait 10 minutes for the user (source: ``timeout: 10 * 60 * 1000``)."""
ASYNC_POLL_SECONDS: Final = 1.0
TASK_INVALID: Final = "MCP error -32602: Invalid task creation result: " + issues_text(
    [type_issue("object", None, ["task"], present=False)]
)
NO_ROOTS: Final = (
    "The client supports roots but no roots are currently configured.\n\n"
    "This could mean:\n"
    "1. The client hasn't provided any roots yet\n"
    "2. The client provided an empty roots list\n"
    "3. The roots configuration is still being loaded"
)
ROOTS_NOTE: Final = (
    "Note: This server demonstrates the roots protocol capability "
    "but doesn't actually access files. "
    "The roots are provided by the MCP client "
    "and can be used by servers that need file system access."
)
LOGGING_STARTED: Final = (
    "Started simulated, random-leveled logging for session {session} at a 5 second pace. "
    "Client's selected logging level will be respected. "
    "If an interval elapses and the message to be "
    "sent is below the selected level, it will not be sent. Thus at higher chosen logging levels, "
    "messages should arrive further apart. "
)
UPDATES_STARTED: Final = (
    "Started simulated resource updated notifications for session {session} at a 5 second pace. "
    "Client will receive updates for any resources the it is subscribed to."
)
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
STATIC_PREFIX: Final = "demo://resource/static/document/"
SESSION_PREFIX: Final = "demo://resource/session/"
DYNAMIC: Final = re.compile(r"^demo://resource/dynamic/(text|blob)/([^/,]+)$")
TEXT_TEMPLATE: Final = "demo://resource/dynamic/text/{resourceId}"
BLOB_TEMPLATE: Final = "demo://resource/dynamic/blob/{resourceId}"
DEPARTMENTS: Final = {
    "Engineering": ["Alice", "Bob", "Charlie"],
    "Sales": ["David", "Eve", "Frank"],
    "Marketing": ["Grace", "Henry", "Iris"],
    "Support": ["John", "Kim", "Lee"],
}
RESOURCE_TYPES: Final = ("Text", "Blob")


# -- shared helpers --------------------------------------------------------------------------------


def _params(ctx: RequestContext) -> JsonObject:
    message = ctx.json_body if isinstance(ctx.json_body, dict) else {}
    params = message.get("params")
    return params if isinstance(params, dict) else {}


def _now(ctx: RequestContext) -> int:
    """The fake clock now (``ctx.now_ns`` is when the request arrived)."""
    return ctx.runtime.clocks.for_session(ctx.session).now_ns()


def _text(text: str) -> JsonObject:
    return {"type": "text", "text": text}


def _result(*texts: str) -> JsonObject:
    return {"content": [_text(t) for t in texts]}


def _root_name(root: JsonObject) -> str:
    name = root.get("name")
    return template(name) if truthy(name) else "Unnamed Root"


def local_time(now_ns: int) -> str:
    """``new Date().toLocaleTimeString()`` on the recorded server: en-US, America/New_York."""
    moment = datetime.fromtimestamp(now_ns / 1e9, UTC).astimezone(SERVER_ZONE)
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d}:{moment.second:02d} {'AM' if moment.hour < 12 else 'PM'}"  # noqa: PLR2004


def dynamic_resource(kind: str, uri: str, number: float, now_ns: int) -> JsonObject:
    """TextResource / blobResource: made up on the spot, stamped with the local time."""
    if kind == "text":
        made = f"created at {local_time(now_ns)}"
        text = f"Resource {number_to_string(number)}: This is a plaintext resource {made}"
        return {"uri": uri, "mimeType": "text/plain", "text": text}
    made = f"created at {local_time(now_ns)}"
    blob = f"Resource {number_to_string(number)}: This is a base64 blob {made}"
    return {"uri": uri, "mimeType": "text/plain", "blob": base64.b64encode(blob.encode()).decode()}


def _client_failed(error: ClientError) -> JsonObject:
    """A tool whose request to the client failed answers with the SDK's McpError message."""
    return tool_error(f"MCP error {error.code}: {error.message}")


_ABSENT: Final = object()


def _task_requested(ctx: RequestContext) -> bool:
    """Whether a tools/call asks for a task (``CallToolRequestSchema`` checks its shape first)."""
    params = _params(ctx)
    if "task" not in params:
        return False
    task = params["task"]
    if not isinstance(task, dict):
        raise protocol_error([type_issue("object", task, ["params", "task"])])
    ttl = task.get("ttl", _ABSENT)
    if ttl is not _ABSENT and not (is_number(ttl) and math.isfinite(to_double(ttl))):  # type: ignore[arg-type]
        raise protocol_error([type_issue("number", ttl, ["params", "task", "ttl"])])
    return True


def _check_related_task(ctx: RequestContext) -> None:
    """A request naming a related task the client doesn't have is refused before it runs.

    The SDK looks the task up first (``_onrequest``): an unknown one answers
    ``Task not found`` (recorded by the parity suite for ``ping``). A request naming an existing
    task runs here and answers as usual; the SDK would queue that answer for tasks/result.
    """
    meta = _params(ctx).get("_meta")
    related = meta.get(tasks.RELATED_TASK) if isinstance(meta, dict) else None
    task_id = related.get("taskId") if isinstance(related, dict) else None
    if not isinstance(task_id, str) or not task_id:
        return
    task = client_state(ctx).tasks.get(task_id)
    if task is None or task.gone(task.time(_now(ctx))):
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Task not found: {task_id}")


def _tool(name: str) -> Callable[[ToolHandler], ToolHandler]:
    """Register a tool handler; a call with ``task`` runs, then fails the SDK's result check."""

    def register(handler: ToolHandler) -> ToolHandler:
        async def run(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
            _check_related_task(ctx)
            augmented = _task_requested(ctx)
            answer = await handler(ctx, arguments)
            if augmented:
                raise RpcError(INVALID_PARAMS, TASK_INVALID)
            return answer

        TOOL_HANDLERS[name] = run
        return handler

    return register


def _method(name: str) -> Callable[[MethodHandler], MethodHandler]:
    """Register a method handler (the front already checked the message: ``front.py``).

    A request (not a notification) first has its related task looked up, as the SDK does.
    """

    def register(handler: MethodHandler) -> MethodHandler:
        async def run(ctx: RequestContext, params: JsonObject) -> JsonValue:
            if isinstance(ctx.json_body, dict) and "id" in ctx.json_body:
                _check_related_task(ctx)
            return await handler(ctx, params)

        METHOD_HANDLERS[name] = run
        return handler

    return register


# -- simple tools ----------------------------------------------------------------------------------

ECHO: Final = (Field("message", "string"),)


@_tool("echo")
async def echo(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Echo: <message>."""
    del ctx
    args, problems = parse_arguments(ECHO, arguments)
    if problems:
        return tool_input_error("echo", problems)
    return _result(f"Echo: {args['message']}")


ANNOTATED_ARGS: Final = (
    Field("messageType", "enum", values=("error", "success", "debug")),
    Field("includeImage", "boolean", default=False),
)


@_tool("get-annotated-message")
async def get_annotated_message(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """A message annotated by type, and optionally the annotated MCP logo."""
    del ctx
    args, problems = parse_arguments(ANNOTATED_ARGS, arguments)
    if problems:
        return tool_input_error("get-annotated-message", problems)
    content: list[JsonValue] = [ANNOTATED[str(args["messageType"])]]
    if args["includeImage"]:
        content.append(
            {
                "type": "image",
                "data": constants.TINY_IMAGE,
                "mimeType": "image/png",
                "annotations": {"audience": ["user"], "priority": 0.5},
            }
        )
    return {"content": content}


@_tool("get-env")
async def get_env(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """The server process's environment, as recorded (it ran with a bare one)."""
    del ctx, arguments
    return _result(constants.ENVIRONMENT_TEXT)


@_tool("get-tiny-image")
async def get_tiny_image(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """The MCP logo between two texts."""
    del ctx, arguments
    return {
        "content": [
            _text("Here's the image you requested:"),
            {"type": "image", "data": constants.TINY_IMAGE, "mimeType": "image/png"},
            _text("The image above is the MCP logo."),
        ]
    }


STRUCTURED: Final = (Field("location", "enum", values=("New York", "Chicago", "Los Angeles")),)


@_tool("get-structured-content")
async def get_structured_content(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Fixed weather per city, as text and as structured content."""
    del ctx
    args, problems = parse_arguments(STRUCTURED, arguments)
    if problems:
        return tool_input_error("get-structured-content", problems)
    weather = WEATHER[str(args["location"])]
    return {"content": [_text(stringify(weather))], "structuredContent": dict(weather)}


SUM: Final = (Field("a", "number"), Field("b", "number"))


@_tool("get-sum")
async def get_sum(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """The sum in IEEE doubles, spelled as JavaScript spells numbers."""
    del ctx
    args, problems = parse_arguments(SUM, arguments)
    if problems:
        return tool_input_error("get-sum", problems)
    a, b = to_double(args["a"]), to_double(args["b"])  # type: ignore[arg-type]
    return _result(
        f"The sum of {number_to_string(a)} and {number_to_string(b)} is {number_to_string(a + b)}."
    )


LINKS: Final = (Field("count", "number", default=3, minimum=1, maximum=10),)


@_tool("get-resource-links")
async def get_resource_links(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Links to dynamic resources 1..count: even ids text, odd ids blob (all text/plain)."""
    del ctx
    args, problems = parse_arguments(LINKS, arguments)
    if problems:
        return tool_input_error("get-resource-links", problems)
    count = to_double(args["count"])  # type: ignore[arg-type]
    content: list[JsonValue] = [
        _text(
            f"Here are {number_to_string(count)} resource links "
            "to resources available in this server:"
        )
    ]
    resource_id = 1
    while resource_id <= count:
        text = resource_id % 2 == 0
        content.append(
            {
                "name": f"{'Text' if text else 'Blob'} Resource {resource_id}",
                "uri": f"demo://resource/dynamic/{'text' if text else 'blob'}/{resource_id}",
                "description": f"Resource {resource_id}: plaintext resource",
                "mimeType": "text/plain",
                "type": "resource_link",
            }
        )
        resource_id += 1
    return {"content": content}


REFERENCE: Final = (
    Field("resourceType", "enum", values=RESOURCE_TYPES, default="Text"),
    Field("resourceId", "number", default=1),
)


@_tool("get-resource-reference")
async def get_resource_reference(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """An embedded dynamic resource and its URI."""
    args, problems = parse_arguments(REFERENCE, arguments)
    if problems:
        return tool_input_error("get-resource-reference", problems)
    number = to_double(args["resourceId"])  # type: ignore[arg-type]
    if not is_integer(number) or number < 1:
        return tool_error(
            f"Invalid resourceId: {number_to_string(number)}. Must be a finite positive integer."
        )
    kind = "text" if args["resourceType"] == "Text" else "blob"
    uri = href(f"demo://resource/dynamic/{kind}/{number_to_string(number)}") or ""
    resource = dynamic_resource(kind, uri, number, _now(ctx))
    return {
        "content": [
            _text(f"Returning resource reference for Resource {number_to_string(number)}:"),
            {"type": "resource", "resource": resource},
            _text(f"You can access this resource using the URI: {uri}"),
        ]
    }


# -- gzip-file-as-resource ------------------------------------------------------------------------

GZIP: Final = (
    Field("name", "string", default="README.md.gz"),
    Field("data", "url", default=DEFAULT_GZIP_URL),
    Field("outputType", "enum", values=("resourceLink", "resource"), default="resourceLink"),
)


class FetchError(Exception):
    """A fetch that fails, with the message the tool reports."""


def _recorded_file(ctx: RequestContext, url: str) -> bytes | None:
    """A remote file the real server fetched while recording (decompressed from what it served)."""
    import gzip  # noqa: PLC0415 - only for the rare remote file

    exchange_id = REMOTE_FILES.get(url)
    exchange = ctx.runtime.recordings.get(exchange_id) if exchange_id else None
    reply = exchange.response.body.parsed() if exchange is not None and exchange.response else None
    result = reply.get("result") if isinstance(reply, dict) else None
    contents = result.get("contents") if isinstance(result, dict) else None
    blob = (
        contents[0].get("blob")
        if isinstance(contents, list) and contents and isinstance(contents[0], dict)
        else None
    )
    return gzip.decompress(base64.b64decode(str(blob))) if blob else None


async def _fetch(ctx: RequestContext, url: str) -> bytes:
    """``fetchSafely``: a ``data:`` URL decoded, a recorded remote file, else ``fetch failed``."""
    location = href(url) or url
    if location.startswith("data:"):
        body = data_url_body(url)
        if body is None:
            raise FetchError("fetch failed")
    else:
        recorded = _recorded_file(ctx, location)
        if recorded is None:  # a fake never reaches the network: the real failure to reach it
            await ctx.pause(FAILED_FETCH_SECONDS)
            raise FetchError("fetch failed")
        await ctx.pause(REMOTE_FETCH_SECONDS)
        body = recorded
    return body  # gzip's 10 MB limit: out of reach (bodies stop at 4 MiB, the README is 8.7 KB)


def register_session_resource(ctx: RequestContext, resource: SessionResource) -> None:
    """registerSessionResource: replacing a URI first removes it (from whichever client has it)."""
    owners = resource_owners(ctx)
    previous = owners.pop(resource.uri, None)
    if previous is not None:
        owner, channel = previous
        owner.resources.pop(resource.uri, None)
        if channel is not None:
            channel.push("notifications/resources/list_changed")
    state = client_state(ctx)
    state.resources[resource.uri] = resource
    owners[resource.uri] = (state, ctx.channel)
    if ctx.channel is not None:
        ctx.channel.push("notifications/resources/list_changed")


@_tool("gzip-file-as-resource")
async def gzip_file_as_resource(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Gzip a file into a resource of this session; return it or a link to it."""
    args, problems = parse_arguments(GZIP, arguments)
    if problems:
        return tool_input_error("gzip-file-as-resource", problems)
    name, data = str(args["name"]), str(args["data"])
    if protocol(data) not in {"http:", "https:", "data:"}:
        return tool_error(
            f"Error processing file {data}: Unsupported URL protocol for {data}. "
            "Only http, https, and data URLs are supported."
        )
    try:
        body = await _fetch(ctx, data)
    except FetchError as error:
        return tool_error(str(error))
    blob = base64.b64encode(node_gzip(body)).decode()
    uri = SESSION_PREFIX + name
    register_session_resource(ctx, SessionResource(uri, name, "application/gzip", blob))
    if args["outputType"] == "resource":
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
            {"name": name, "uri": uri, "mimeType": "application/gzip", "type": "resource_link"}
        ]
    }


# -- simulated logging and resource updates --------------------------------------------------------


def _session_label(ctx: RequestContext) -> str:
    return template(ctx.mcp_session) if ctx.mcp_session else "undefined"


@_tool("toggle-simulated-logging")
async def toggle_simulated_logging(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Start (or stop) a random-leveled log message every 5 s on the client's GET stream."""
    del arguments
    state = client_state(ctx)
    session = _session_label(ctx)
    if state.logging is not None:
        state.logging.cancel()
        state.logging = None
        return _result(f"Stopped simulated logging for session {session}")
    suffix = f" - SessionId {ctx.mcp_session}" if ctx.mcp_session else ""
    messages = [
        ("debug", f"Debug-level message{suffix}"),
        ("info", f"Info-level message{suffix}"),
        ("notice", f"Notice-level message{suffix}"),
        ("warning", f"Warning-level message{suffix}"),
        ("error", f"Error-level message{suffix}"),
        ("critical", f"Critical-level message{suffix}"),
        ("alert", f"Alert level-message{suffix}"),
        ("emergency", f"Emergency-level message{suffix}"),
    ]
    rng = random.Random(ctx.rng.random())
    current = is_current(ctx, state)

    async def work(channel: Channel) -> None:
        while current():  # a reset of the fake ends what the client left running
            level, data = rng.choice(messages)
            log(state, channel, level, data)
            await channel.pause(SIMULATION_PERIOD)

    state.logging = ctx.background(work)
    return _result(LOGGING_STARTED.format(session=session))


@_tool("toggle-subscriber-updates")
async def toggle_subscriber_updates(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Start (or stop) resources/updated pushes every 5 s for each URI the client subscribed to."""
    del arguments
    state = client_state(ctx)
    session = _session_label(ctx)
    if state.updates is not None:
        state.updates.cancel()
        state.updates = None
        return _result(f"Stopped simulated resource updates for session {session}")

    current = is_current(ctx, state)
    shared, client = subscribers(ctx), ctx.mcp_session or ""

    async def work(channel: Channel) -> None:
        while current():  # a reset of the fake ends what the client left running
            for uri, sessions in list(shared.items()):
                if client in sessions:
                    channel.push("notifications/resources/updated", {"uri": uri})
            await channel.pause(SIMULATION_PERIOD)

    state.updates = ctx.background(work)
    return _result(UPDATES_STARTED.format(session=session))


async def _subscription(ctx: RequestContext, params: JsonObject, *, subscribe: bool) -> JsonValue:
    uri = require_string(params, "uri")
    state = client_state(ctx)
    origin = f"from session {ctx.mcp_session}" if ctx.mcp_session else ""
    if subscribe:
        log(
            state,
            ctx.channel,
            "info",
            f"Received Subscribe Resource request for URI: {uri} {origin}",
        )
        subscribers(ctx).setdefault(uri, set()).add(ctx.mcp_session or "")
    else:
        log(state, ctx.channel, "info", f"Received Unsubscribe Resource request: {uri} {origin}")
        subscribers(ctx).get(uri, set()).discard(ctx.mcp_session or "")
    return {}


@_method("resources/subscribe")
async def subscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Subscribe to a resource's updates (logged to the client)."""
    return await _subscription(ctx, params, subscribe=True)


@_method("resources/unsubscribe")
async def unsubscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Unsubscribe from a resource's updates (logged to the client)."""
    return await _subscription(ctx, params, subscribe=False)


@_method("logging/setLevel")
async def set_level(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """The least severe level the client wants log messages at."""
    level = params.get("level")
    if level not in LOG_LEVELS:
        raise protocol_error([value_issue(list(LOG_LEVELS), ["params", "level"])])
    client_state(ctx).log_level = str(level)
    return {}


# -- trigger-long-running-operation ---------------------------------------------------------------

LONG: Final = (Field("duration", "number", default=10), Field("steps", "number", default=5))
TIMEOUT_MAX_MS: Final = 2**31 - 1


def node_timer_ms(delay_ms: float) -> float:
    """``setTimeout``'s delay as Node uses it: below 1 ms, NaN or above 2^31-1 ms it is 1 ms."""
    return delay_ms if 1 <= delay_ms <= TIMEOUT_MAX_MS else 1.0


@_tool("trigger-long-running-operation")
async def trigger_long_running_operation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Wait ``duration`` in ``steps`` steps, with a progress notification per step."""
    args, problems = parse_arguments(LONG, arguments)
    if problems:
        return tool_input_error("trigger-long-running-operation", problems)
    duration, steps = to_double(args["duration"]), to_double(args["steps"])  # type: ignore[arg-type]
    meta = _params(ctx).get("_meta")
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    step = 1
    while step < steps + 1:
        await ctx.pause(node_timer_ms(duration / steps * 1000) / 1000)
        if token is not None:
            ctx.notify(
                "notifications/progress",
                {"progress": step, "total": json_number(steps), "progressToken": token},
            )
        step += 1
    return _result(
        "Long running operation completed. "
        f"Duration: {number_to_string(duration)} seconds, Steps: {number_to_string(steps)}."
    )


# -- requests to the client: sampling, elicitation, roots -----------------------------------------

SAMPLING: Final = (Field("prompt", "string"), Field("maxTokens", "number", default=100))


def _sampling_request(
    tool: str, prompt: str, max_tokens: JsonValue, *, task: bool = False
) -> JsonObject:
    params: JsonObject = {"task": {"ttl": 300000}} if task else {}
    params["messages"] = [
        {"role": "user", "content": {"type": "text", "text": f"Resource {tool} context: {prompt}"}}
    ]
    params["systemPrompt"] = "You are a helpful test server."
    params["maxTokens"] = json_number(to_double(max_tokens))  # type: ignore[arg-type]
    params["temperature"] = 0.7
    return params


@_tool("trigger-sampling-request")
async def trigger_sampling_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Ask the client to sample; print its result as the SDK parsed it."""
    args, problems = parse_arguments(SAMPLING, arguments)
    if problems:
        return tool_input_error("trigger-sampling-request", problems)
    request = _sampling_request("trigger-sampling-request", str(args["prompt"]), args["maxTokens"])
    try:
        reply = await ctx.ask_client("sampling/createMessage", request, within=SDK_REQUEST_TIMEOUT)
    except ClientError as error:
        return _client_failed(error)
    parsed, issues = create_message_result(reply)
    if issues:
        return tool_error(issues_text(issues))
    return _result(f"LLM sampling result: \n{stringify(parsed, 2)}")


def _elicitation_lines(content: JsonObject, fields: tuple[tuple[str, str, bool], ...]) -> list[str]:
    """``- Label: value`` lines: ``truthy`` fields when truthy, the others whenever present."""
    lines = []
    for key, label, needs_truth in fields:
        value = content.get(key)
        if truthy(value) if needs_truth else key in content:
            lines.append(f"- {label}: {template(value)}")
    return lines


FORM_FIELDS: Final = (
    ("name", "Name", True),
    ("check", "Agreed to terms", False),
    ("color", "Favorite Color", True),
    ("email", "Email", True),
    ("homepage", "Homepage", True),
    ("birthdate", "Birthdate", True),
    ("integer", "Favorite Integer", False),
    ("number", "Favorite Number", False),
    ("petType", "Pet Type", True),
)


@_tool("trigger-elicitation-request")
async def trigger_elicitation_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Ask the user (form elicitation) and report what they did."""
    del arguments
    try:
        reply = await ctx.ask_client(
            "elicitation/create", dict(constants.FORM_ELICITATION), within=ELICITATION_TIMEOUT
        )
    except ClientError as error:
        return _client_failed(error)
    parsed, issues = elicit_result(reply)
    if issues:
        return tool_error(issues_text(issues))
    content: list[JsonValue] = []
    action, given = parsed.get("action"), parsed.get("content")
    if action == "accept" and truthy(given) and isinstance(given, dict):
        content.append(_text("✅ User provided the requested information!"))
        content.append(_text("User inputs:\n" + "\n".join(_elicitation_lines(given, FORM_FIELDS))))
    elif action == "decline":
        content.append(_text("❌ User declined to provide the requested information."))
    elif action == "cancel":
        content.append(_text("⚠️ User cancelled the elicitation dialog."))
    content.append(_text(f"\nRaw result: {stringify(parsed, 2)}"))
    return {"content": content}


URL_ELICITATION: Final = (
    Field("url", "url"),
    Field("message", "string", default="Please open the link to complete this action."),
    Field("elicitationId", "string", optional=True),
    Field("errorPath", "boolean", default=False),
)


@_tool("trigger-url-elicitation")
async def trigger_url_elicitation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """URL elicitation by request (elicitation/create) or by error (-32042, then the retry)."""
    args, problems = parse_arguments(URL_ELICITATION, arguments)
    if problems:
        return tool_input_error("trigger-url-elicitation", problems)
    url, requested = str(args["url"]), args.get("elicitationId")
    elicitation_id = str(requested) if requested is not None else str(uuid.uuid4())
    key = f"{ctx.mcp_session or 'default'}\x00{url}\x00{requested or ''}"
    state = client_state(ctx)
    if args["errorPath"]:
        if key in state.url_markers:
            state.url_markers.discard(key)
        else:
            state.url_markers.add(key)
            prerequisite: JsonObject = {
                "mode": "url",
                "url": "https://modelcontextprotocol.io",
                "message": "Open this link to satisfy the prerequisite, then retry the request.",
                "elicitationId": str(uuid.uuid4()),
            }
            raise RpcError(
                -32042,
                "MCP error -32042: This request requires browser-based authorization.",
                {"elicitations": [prerequisite]},
            )
    request: JsonObject = {
        "mode": "url",
        "url": url,
        "message": args["message"],
        "elicitationId": elicitation_id,
    }
    try:
        reply = await ctx.ask_client("elicitation/create", request, within=ELICITATION_TIMEOUT)
    except ClientError as error:
        return _client_failed(error)
    parsed, issues = elicit_result(reply)
    if issues:
        return tool_error(issues_text(issues))
    content: list[JsonValue] = []
    match parsed.get("action"):
        case "accept":
            content.append(
                _text(
                    "✅ User completed the URL elicitation flow.\n"
                    f"Elicitation ID: {elicitation_id}\nURL: {url}"
                )
            )
        case "decline":
            content.append(
                _text(f"❌ User declined to open the URL (Elicitation ID: {elicitation_id}).")
            )
        case _:
            content.append(
                _text(f"⚠️ User cancelled the URL elicitation (Elicitation ID: {elicitation_id}).")
            )
    content.append(_text(f"\nRaw result: {stringify(parsed, 2)}"))
    return {"content": content}


async def request_roots(ctx: RequestContext, state: ClientState, channel: Channel | None) -> None:
    """requestRoots: ask on the client's GET stream; keep what a valid answer says, and log it.

    The real server sends ``roots/list`` outside any call, so it reaches only a client holding its
    GET stream open; without one the request is lost and the SDK gives up after 60 s.
    """
    if channel is None:
        return
    started = time.monotonic()
    try:
        reply = await channel.ask("roots/list", None, within=SDK_REQUEST_TIMEOUT)
    except ClientError as error:
        waited = time.monotonic() - started
        if error.code == -32001 and waited < SDK_REQUEST_TIMEOUT:  # noqa: PLR2004 - lost: no stream
            await ctx.pause(SDK_REQUEST_TIMEOUT - waited)
        return
    roots = roots_result(reply)
    if roots is None:
        return
    state.roots = roots
    log(
        state,
        channel,
        "info",
        f"Roots updated: {len(roots)} root(s) received from client",
        logger="everything-server",
    )


@_tool("get-roots-list")
async def get_roots_list(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """The roots the client reported (asked for once, when not known yet)."""
    del arguments
    state = client_state(ctx)
    if state.roots is None:
        state.roots_watch = True
        await request_roots(ctx, state, ctx.channel)
    roots = state.roots or []
    if not roots:
        return _result(NO_ROOTS)
    listing = "\n\n".join(
        f"{index + 1}. {_root_name(root)}\n   URI: {root['uri']}"
        for index, root in enumerate(roots)
    )
    return _result(f"Current MCP Roots ({len(roots)} total):\n\n{listing}\n\n{ROOTS_NOTE}")


@_method("notifications/initialized")
async def initialized(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """The client initialized: the server registers the tools its capabilities allow.

    Each registration sends ``notifications/tools/list_changed`` (``registerConditionalTools``,
    SDK ``McpServer.registerTool``): on the client's GET stream if one is already open (clients
    usually open it later, and miss them). 350 ms later, the server asks a roots-capable client
    for its roots.
    """
    del params
    for _ in range(_registered_on_initialized(ctx)):
        ctx.push("notifications/tools/list_changed")
    if "roots" not in ctx.client_capabilities:
        return None
    await ctx.pause(ROOTS_SYNC_DELAY)
    state = client_state(ctx)
    if state.roots is None:
        state.roots_watch = True
        await request_roots(ctx, state, ctx.channel)
    return None


def _registered_on_initialized(ctx: RequestContext) -> int:
    """How many tools the server registers for this client once it initialized (``requires``)."""
    mcp = ctx.runtime.model.mcp
    tools = mcp.tools if mcp is not None else []
    return sum(1 for tool in tools if tool.requires and tool.offered_to(ctx.client_capabilities))


@_method("notifications/roots/list_changed")
async def roots_changed(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """The client's roots changed: ask again (once the server set up its handler)."""
    del params
    state = client_state(ctx)
    if state.roots_watch:
        await request_roots(ctx, state, ctx.channel)
    return None


# -- the async (client task) tools ----------------------------------------------------------------


class RefusedReplyError(Exception):
    """A client's answer the tool's zod schema refuses: the tool reports the issues."""

    def __init__(self, issues: list[JsonObject]) -> None:
        super().__init__(issues_text(issues))
        self.issues = issues


def _client_task(reply: JsonValue, kind: str) -> tuple[JsonObject | None, JsonObject]:
    """The client's task, else its synchronous answer (as the tool's union schema keeps it)."""
    task, answer, issues = client_task_reply(reply, kind)
    if issues:
        raise RefusedReplyError(issues)
    return task, answer


async def _poll_client_task(
    ctx: RequestContext, task: JsonObject, limit: int, *, quiet: bool
) -> tuple[str, str | None, int, list[str]]:
    """Poll the client's tasks/get each second until terminal: (status, message, polls, log)."""
    task_id = str(task["taskId"])
    progress = [f"Task created: {task_id}"]
    status, message, attempts = str(task["status"]), None, 0
    while status not in tasks.TERMINAL and attempts < limit:
        await ctx.pause(ASYNC_POLL_SECONDS)
        attempts += 1
        polled = await ctx.ask_client("tasks/get", {"taskId": task_id}, within=SDK_REQUEST_TIMEOUT)
        status, message, issues = poll_reply(polled)
        if issues:
            raise RefusedReplyError(issues)
        if not quiet or attempts == 1 or attempts % 10 == 0 or status != "input_required":
            progress.append(f"Poll {attempts}: {status}{f' - {message}' if message else ''}")
    return status, message, attempts, progress


@_tool("trigger-sampling-request-async")
async def trigger_sampling_request_async(  # noqa: PLR0911 - one answer per outcome
    ctx: RequestContext, arguments: JsonObject
) -> JsonValue:
    """Ask the client to sample as a task, poll its task, then fetch the result."""
    args, problems = parse_arguments(SAMPLING, arguments)
    if problems:
        return tool_input_error("trigger-sampling-request-async", problems)
    request = _sampling_request(
        "trigger-sampling-request-async", str(args["prompt"]), args["maxTokens"], task=True
    )
    try:
        reply = await ctx.ask_client("sampling/createMessage", request, within=SDK_REQUEST_TIMEOUT)
        task, synced = _client_task(reply, "sampling")
        if task is None:
            return _result(f"[SYNC] Client executed synchronously:\n{stringify(synced, 2)}")
        status, message, attempts, progress = await _poll_client_task(ctx, task, 60, quiet=False)
        if attempts >= 60:  # noqa: PLR2004
            return _result(
                "[TIMEOUT] Task timed out after 60 poll attempts\n\nProgress:\n"
                + "\n".join(progress)
            )
        if status in {"failed", "cancelled"}:
            return _result(
                f"[{status.upper()}] {message or 'No message'}\n\nProgress:\n" + "\n".join(progress)
            )
        result = await ctx.ask_client(
            "tasks/result", {"taskId": task["taskId"]}, within=SDK_REQUEST_TIMEOUT
        )
    except ClientError as error:
        return _client_failed(error)
    except RefusedReplyError as refused:
        return tool_error(str(refused))
    return _result(
        "[COMPLETED] Async sampling completed!\n\n**Progress:**\n"
        + "\n".join(progress)
        + f"\n\n**Result:**\n{stringify(result, 2)}"
    )


ASYNC_FIELDS: Final = (
    ("name", "Name", True),
    ("favoriteColor", "Favorite Color", True),
    ("agreeToTerms", "Agreed to terms", False),
)


@_tool("trigger-elicitation-request-async")
async def trigger_elicitation_request_async(
    ctx: RequestContext, arguments: JsonObject
) -> JsonValue:
    """Ask the user as a client task, poll it (up to 10 minutes), then fetch the answer."""
    del arguments
    request: JsonObject = {"task": {"ttl": 600000}, **constants.ASYNC_FORM_ELICITATION}
    try:
        reply = await ctx.ask_client("elicitation/create", request, within=SDK_REQUEST_TIMEOUT)
        task, synced = _client_task(reply, "elicitation")
        if task is None:
            return _result(f"[SYNC] Client executed synchronously:\n{stringify(synced, 2)}")
        status, message, attempts, progress = await _poll_client_task(ctx, task, 600, quiet=True)
        if attempts >= 600:  # noqa: PLR2004
            return _result(
                "[TIMEOUT] Task timed out after 600 poll attempts\n\nProgress:\n"
                + "\n".join(progress)
            )
        if status in {"failed", "cancelled"}:
            return _result(
                f"[{status.upper()}] {message or 'No message'}\n\nProgress:\n" + "\n".join(progress)
            )
        result = await ctx.ask_client(
            "tasks/result", {"taskId": task["taskId"]}, within=SDK_REQUEST_TIMEOUT
        )
    except ClientError as error:
        return _client_failed(error)
    except RefusedReplyError as refused:
        return tool_error(str(refused))
    answer = result if isinstance(result, dict) else {}
    content: list[JsonValue] = []
    given = answer.get("content")
    if answer.get("action") == "accept" and truthy(given) and isinstance(given, dict):
        content.append(_text("[COMPLETED] User provided the requested information!"))
        content.append(_text("User inputs:\n" + "\n".join(_elicitation_lines(given, ASYNC_FIELDS))))
    elif answer.get("action") == "decline":
        content.append(_text("[DECLINED] User declined to provide the requested information."))
    elif answer.get("action") == "cancel":
        content.append(_text("[CANCELLED] User cancelled the elicitation dialog."))
    content.append(
        _text("\nProgress:\n" + "\n".join(progress) + f"\n\nRaw result: {stringify(result, 2)}")
    )
    return {"content": content}


# -- simulate-research-query and the tasks methods -------------------------------------------------

RESEARCH: Final = (Field("topic", "string"), Field("ambiguous", "boolean", default=False))


async def simulate_research_query(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Create a research task (the tool requires task augmentation)."""
    _check_related_task(ctx)
    if not _task_requested(ctx):
        return tool_error(
            "MCP error -32601: Tool simulate-research-query requires task augmentation "
            "(taskSupport: 'required')"
        )
    args, problems = parse_arguments(RESEARCH, arguments)
    if problems:
        raise RpcError(INVALID_PARAMS, TASK_INVALID)
    can_elicit = "elicitation" in ctx.client_capabilities
    task = tasks.ResearchTask.new(
        str(args["topic"]), ambiguous=bool(args["ambiguous"]) and can_elicit, now_ns=ctx.now_ns
    )
    client_state(ctx).tasks[task.task_id] = task
    clock = ctx.runtime.clocks.for_session(ctx.session).now_ns
    current = is_current(ctx, client_state(ctx))
    ctx.background(lambda channel: tasks.push_statuses(task, clock, channel, current))
    return {"task": task.to_json(task.at(ctx.now_ns))}


TOOL_HANDLERS["simulate-research-query"] = simulate_research_query


def _task(ctx: RequestContext, params: JsonObject, missing: str) -> tasks.ResearchTask:
    """The client's task named in ``params``, or the SDK's refusal."""
    task_id = require_string(params, "taskId")
    task = client_state(ctx).tasks.get(task_id)
    now = task.time(_now(ctx)) if task is not None else 0
    if task is None or task.gone(now):
        raise RpcError(INVALID_PARAMS, missing.format(task_id=task_id))
    task.settle(now)
    return task


@_method("tasks/get")
async def tasks_get(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """A task's current status."""
    task = _task(ctx, params, "MCP error -32602: Failed to retrieve task: Task not found")
    return task.to_json(task.at(task.time(_now(ctx))))


@_method("tasks/result")
async def tasks_result(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """A task's result, once terminal (delivering its queued question first)."""
    task = _task(ctx, params, "MCP error -32602: Task not found: {task_id}")
    return await tasks.task_result(ctx, task)


@_method("tasks/list")
async def tasks_list(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """This client's tasks, ten per page."""
    cursor = params.get("cursor", _ABSENT)
    if cursor is not _ABSENT and not isinstance(cursor, str):
        raise protocol_error([type_issue("string", cursor, ["params", "cursor"])])
    known = list(client_state(ctx).tasks.values())
    now = _now(ctx)
    for task in known:
        task.settle(task.time(now))
    return tasks.page(known, params.get("cursor"), now)


@_method("tasks/cancel")
async def tasks_cancel(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Cancel a task that has not ended."""
    task = _task(ctx, params, "MCP error -32602: Task not found: {task_id}")
    now = task.time(_now(ctx))
    status = task.at(now)
    if status.status in tasks.TERMINAL:
        raise RpcError(
            INVALID_PARAMS,
            f"MCP error -32602: Cannot cancel task in terminal status: {status.status}",
        )
    task.cancelled_ns = now
    task.answered.set()  # a pending question is dropped with the task
    return {"_meta": {}, **task.to_json(task.at(now))}


# -- resources -------------------------------------------------------------------------------------


def _static_documents(ctx: RequestContext) -> dict[str, JsonObject]:
    """The static documents (served from their recorded reads), by URI."""
    found: dict[str, JsonObject] = {}
    model = ctx.runtime.model.mcp
    for resource in model.resources if model is not None else []:
        uri = resource.get("uri") if isinstance(resource, dict) else None
        exchanges = ctx.runtime.recordings.for_endpoint(f"MCP resources/read:{uri}") if uri else []
        for exchange in exchanges:
            reply = exchange.response.body.parsed() if exchange.response else None
            result = reply.get("result") if isinstance(reply, dict) else None
            contents = result.get("contents") if isinstance(result, dict) else None
            if isinstance(contents, list) and contents and isinstance(contents[0], dict):
                found[str(uri)] = contents[0]
                break
    return found


@_method("resources/list")
async def resources_list(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """The static documents, then this client's session resources."""
    del params
    model = ctx.runtime.model.mcp
    listed: list[JsonValue] = list(model.resources) if model is not None else []
    listed.extend(
        {"uri": resource.uri, "name": resource.name, "mimeType": resource.mime_type}
        for resource in client_state(ctx).resources.values()
    )
    return {"resources": listed}


@_method("resources/read")
async def resources_read(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """A registered resource (static or this session's), else a dynamic one by template."""
    raw = require_string(params, "uri")
    uri = href(raw)
    if uri is None:
        raise RpcError(INTERNAL_ERROR, "Invalid URL")
    documents = _static_documents(ctx)
    if uri in documents:
        return {"contents": [{**documents[uri], "uri": uri}]}
    session_resource = client_state(ctx).resources.get(uri)
    if session_resource is not None:
        return {
            "contents": [
                {
                    "uri": session_resource.uri,
                    "mimeType": session_resource.mime_type,
                    "blob": session_resource.blob,
                }
            ]
        }
    matched = DYNAMIC.match(uri)
    if matched is None:
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Resource {uri} not found")
    number = parse_number(matched.group(2))
    if not is_integer(number) or number <= 0:
        raise RpcError(INTERNAL_ERROR, f"Unknown resource: {uri}")
    return {"contents": [dynamic_resource(matched.group(1), uri, number, _now(ctx))]}


# -- prompts and completions -----------------------------------------------------------------------

PROMPT_ARGS: Final = {
    "args-prompt": (Field("city", "string"), Field("state", "string", optional=True)),
    "completable-prompt": (Field("department", "string"), Field("name", "string")),
    "resource-prompt": (Field("resourceType", "string"), Field("resourceId", "string")),
}


def _prompt_arguments(params: JsonObject) -> JsonObject:
    """``params.arguments`` as the protocol allows it: a record of strings."""
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise protocol_error([type_issue("record", arguments, ["params", "arguments"])])
    issues = [
        type_issue("string", value, ["params", "arguments", key])
        for key, value in arguments.items()
        if not isinstance(value, str)
    ]
    if issues:
        raise protocol_error(issues)
    return arguments


def _user(content: JsonObject) -> JsonObject:
    return {"role": "user", "content": content}


@_method("prompts/get")
async def prompts_get(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """One of the four prompts, filled in."""
    name = require_string(params, "name")
    given = _prompt_arguments(params)
    if name == "simple-prompt":
        return {"messages": [_user(_text("This is a simple prompt without arguments."))]}
    fields = PROMPT_ARGS.get(name)
    if fields is None:
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Prompt {name} not found")
    args, problems = parse_arguments(fields, given)
    if problems:
        raise RpcError(
            INVALID_PARAMS,
            f"MCP error -32602: Invalid arguments for prompt {name}: " + "\n".join(problems),
        )
    if name == "args-prompt":
        state = args.get("state")
        location = f"{args['city']}{f', {state}' if truthy(state) else ''}"
        return {"messages": [_user(_text(f"What's weather in {location}?"))]}
    if name == "completable-prompt":
        return {
            "messages": [
                _user(
                    _text(
                        f"Please promote {args['name']} "
                        f"to the head of the {args['department']} team."
                    )
                )
            ]
        }
    kind, raw_id = str(args["resourceType"]), str(args["resourceId"])
    if kind not in RESOURCE_TYPES:
        raise RpcError(INTERNAL_ERROR, f"Invalid resourceType: {kind}. Must be Text or Blob.")
    number = parse_number(raw_id)
    if not is_integer(number) or number < 1:
        raise RpcError(
            INTERNAL_ERROR, f"Invalid resourceId: {raw_id}. Must be a finite positive integer."
        )
    lower = kind.lower()
    uri = href(f"demo://resource/dynamic/{lower}/{number_to_string(number)}") or ""
    intro = (
        f"This prompt includes the {kind} resource with id: {number_to_string(number)}. "
        "Please analyze the following resource:"
    )
    resource = dynamic_resource(lower, uri, number, _now(ctx))
    return {"messages": [_user(_text(intro)), _user({"type": "resource", "resource": resource})]}


MAX_COMPLETIONS: Final = 100
EMPTY_COMPLETION: Final[JsonObject] = {"completion": {"values": [], "hasMore": False}}


def _completed(values: list[str]) -> JsonObject:
    shown: list[JsonValue] = list(values[:MAX_COMPLETIONS])
    return {
        "completion": {
            "values": shown,
            "total": len(values),
            "hasMore": len(values) > MAX_COMPLETIONS,
        }
    }


def _positive_integer(value: str) -> list[str]:
    number = parse_number(value)
    return [value] if is_integer(number) and number > 0 else []


def _ref_issues(ref: JsonValue) -> JsonObject | None:
    """The protocol's check of a completion reference (a prompt or a resource template)."""
    branches: list[JsonValue] = []
    for kind, key in (("ref/prompt", "name"), ("ref/resource", "uri")):
        found: list[JsonValue] = []
        if not isinstance(ref, dict):
            found.append(type_issue("object", ref, []))
        else:
            if ref.get("type") != kind:
                found.append(
                    {
                        "code": "invalid_value",
                        "values": [kind],
                        "path": ["type"],
                        "message": f'Invalid input: expected "{kind}"',
                    }
                )
            if not isinstance(ref.get(key), str):
                found.append(type_issue("string", ref.get(key), [key], present=key in ref))
        if not found:
            return None
        branches.append(found)
    return {
        "code": "invalid_union",
        "errors": branches,
        "path": ["params", "ref"],
        "message": "Invalid input",
    }


def _completion_issues(params: JsonObject) -> list[JsonObject]:
    """``CompleteRequestParamsSchema``: ref, then argument {name, value}, then context."""
    issues: list[JsonObject] = []
    ref_issue = _ref_issues(params.get("ref"))
    if ref_issue is not None:
        issues.append(ref_issue)
    argument = params.get("argument", _ABSENT)
    if not isinstance(argument, dict):
        path: list[JsonValue] = ["params", "argument"]
        issues.append(type_issue("object", argument, path, present=argument is not _ABSENT))
    else:
        for key in ("name", "value"):
            value = argument.get(key, _ABSENT)
            if not isinstance(value, str):
                issues.append(
                    type_issue(
                        "string", value, ["params", "argument", key], present=value is not _ABSENT
                    )
                )
    context = params.get("context", _ABSENT)
    if context is _ABSENT:
        return issues
    if not isinstance(context, dict):
        return [*issues, type_issue("object", context, ["params", "context"])]
    known = context.get("arguments", _ABSENT)
    if known is _ABSENT:
        return issues
    if not isinstance(known, dict):
        return [*issues, type_issue("record", known, ["params", "context", "arguments"])]
    issues += [
        type_issue("string", value, ["params", "context", "arguments", key])
        for key, value in known.items()
        if not isinstance(value, str)
    ]
    return issues


@_method("completion/complete")
async def complete(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Completions of prompt arguments and resource template variables."""
    issues = _completion_issues(params)
    if issues:
        raise protocol_error(issues)
    ref, argument = params["ref"], params["argument"]
    assert isinstance(argument, dict)  # noqa: S101 - checked above
    assert isinstance(ref, dict)  # noqa: S101 - checked above
    name, value = template(argument.get("name")), template(argument.get("value"))
    context = params.get("context")
    known = context.get("arguments") if isinstance(context, dict) else None
    if ref["type"] == "ref/prompt":
        return _prompt_completion(str(ref["name"]), name, value, known)
    uri = str(ref["uri"])
    if uri in {TEXT_TEMPLATE, BLOB_TEMPLATE}:
        return _completed(_positive_integer(value)) if name == "resourceId" else EMPTY_COMPLETION
    fixed = uri.startswith(STATIC_PREFIX) and uri.removeprefix(STATIC_PREFIX) in STATIC_DOCUMENTS
    if fixed or uri in client_state(ctx).resources:  # a fixed resource: nothing to complete
        return EMPTY_COMPLETION
    raise RpcError(INVALID_PARAMS, f"MCP error -32602: Resource template {uri} not found")


def _prompt_completion(prompt: str, name: str, value: str, known: JsonValue) -> JsonValue:
    """The completers of the prompts' arguments (only two prompts have any)."""
    if prompt not in {"simple-prompt", *PROMPT_ARGS}:
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Prompt {prompt} not found")
    choices: list[str] | None = None
    if prompt == "completable-prompt" and name == "department":
        choices = list(DEPARTMENTS)
    elif prompt == "completable-prompt" and name == "name":
        department = known.get("department") if isinstance(known, dict) else None
        choices = DEPARTMENTS.get(department, []) if isinstance(department, str) else []
    elif prompt == "resource-prompt" and name == "resourceType":
        choices = list(RESOURCE_TYPES)
    elif prompt == "resource-prompt" and name == "resourceId":
        return _completed(_positive_integer(value))
    if choices is None:
        return EMPTY_COMPLETION
    return _completed([choice for choice in choices if choice.startswith(value)])


STATIC_DOCUMENTS: Final = frozenset(
    {
        "architecture.md",
        "extension.md",
        "features.md",
        "how-it-works.md",
        "instructions.md",
        "startup.md",
        "structure.md",
    }
)
"""The documents the server exposes as static resources (resources/list)."""
