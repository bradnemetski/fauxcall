"""How the fake answers the Everything Reference Server's tools and methods.

Every tool except ``get-env`` and ``get-tiny-image`` (replayed: they answer any arguments the
same) is computed here, from the recordings and the server's published source
(``@modelcontextprotocol/server-everything`` 2026.8.31 on the TypeScript SDK 1.32.2): argument
checks worded as zod words them, JavaScript number spelling, the server's local time in dynamic
resources, per-session state (session resources, subscriptions, logging level, roots, toggles,
tasks) and the requests the server sends the client (sampling, elicitation, roots). Static text
that was recorded (the documents, the README the gzip tool fetches) is read from the recordings.
"""

from __future__ import annotations

import base64
import gzip as gzip_module
import json
import math
import re
import struct
import uuid
import zlib
from typing import TYPE_CHECKING, Any, Final

from fauxcall.runtime import RpcError
from fauxcall.runtime.mcp_server import ClientError

from fauxcall_everything import jsvalues, research, sessions, urls, zodlike
from fauxcall_everything.constants import ASYNC_ELICITATION_PARAMS, ELICITATION_PARAMS, TINY_IMAGE
from fauxcall_everything.zodlike import Arg

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

ELICITATION_TIMEOUT_S: Final = 600.0
"""``trigger-elicitation-request`` and ``trigger-url-elicitation`` wait 10 minutes for the user."""
TASK_POLL_S: Final = 1.0
SAMPLING_POLLS: Final = 60
ELICITATION_POLLS: Final = 600
MAX_TIMER_MS: Final = 2**31 - 1
"""Node fires a ``setTimeout`` longer than this after 1 ms (TimeoutOverflowWarning), as it
does one shorter than 1 ms."""
README_URL: Final = (
    "https://raw.githubusercontent.com/modelcontextprotocol/servers/refs/heads/main/README.md"
)
GZIP_OS_MACOS: Final = 19
GZIP_OVERHEAD_MS: Final = 6.0
"""A gzip call's own time without a network fetch (recorded p50 of data: URLs)."""
"""Node's zlib writes OS byte 19 in its gzip header on macOS (recordings 000159-000166)."""
_TERMINAL: Final = frozenset({"completed", "failed", "cancelled"})


def text(value: str) -> JsonObject:
    """One text content block."""
    return {"type": "text", "text": value}


def _client_failure(error: ClientError) -> JsonObject:
    """A request to the client failed (refused, timed out): the tool reports the McpError."""
    return zodlike.tool_error(f"MCP error {error.code}: {error.message}")


def _issues_text(issues: list[JsonObject]) -> JsonObject:
    """A zod error thrown inside a tool: its message is the issue list as JSON."""
    return zodlike.tool_error(json.dumps(issues, indent=2, ensure_ascii=False))


def _uuid(ctx: RequestContext) -> str:
    return str(uuid.UUID(int=ctx.rng.getrandbits(128), version=4))


# -- simple tools ---------------------------------------------------------------------------------

ECHO_ARGS: Final = (Arg("message"),)


async def echo(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``echo``: ``Echo: <message>``."""
    del ctx
    parsed, issues = zodlike.check(ECHO_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("echo", issues)
    return {"content": [text(f"Echo: {parsed['message']}")]}


SUM_ARGS: Final = (Arg("a", "number"), Arg("b", "number"))


async def get_sum(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-sum``: the sum, spelled as JavaScript spells numbers."""
    del ctx
    parsed, issues = zodlike.check(SUM_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("get-sum", issues)
    a, b = jsvalues.to_double(parsed["a"]), jsvalues.to_double(parsed["b"])  # type: ignore[arg-type]
    shown = jsvalues.number_text
    return {"content": [text(f"The sum of {shown(a)} and {shown(b)} is {shown(a + b)}.")]}


ANNOTATED_ARGS: Final = (
    Arg("messageType", "enum", options=("error", "success", "debug")),
    Arg("includeImage", "boolean", default=False),
)
ANNOTATED: Final[dict[str, JsonObject]] = {
    "error": text("Error: Operation failed")
    | {"annotations": {"audience": ["user", "assistant"], "priority": 1}},
    "success": text("Operation completed successfully")
    | {"annotations": {"audience": ["user"], "priority": 0.7}},
    "debug": text("Debug: Cache hit ratio 0.95, latency 150ms")
    | {"annotations": {"audience": ["assistant"], "priority": 0.3}},
}


async def get_annotated_message(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-annotated-message``: a message with audience/priority annotations, maybe an image."""
    del ctx
    parsed, issues = zodlike.check(ANNOTATED_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("get-annotated-message", issues)
    content: list[JsonValue] = [dict(ANNOTATED[str(parsed["messageType"])])]
    if parsed["includeImage"]:
        content.append(
            {
                "type": "image",
                "data": TINY_IMAGE,
                "mimeType": "image/png",
                "annotations": {"audience": ["user"], "priority": 0.5},
            }
        )
    return {"content": content}


WEATHER: Final[dict[str, JsonObject]] = {
    "New York": {"temperature": 33, "conditions": "Cloudy", "humidity": 82},
    "Chicago": {"temperature": 36, "conditions": "Light rain / drizzle", "humidity": 82},
    "Los Angeles": {"temperature": 73, "conditions": "Sunny / Clear", "humidity": 48},
}
WEATHER_ARGS: Final = (Arg("location", "enum", options=tuple(WEATHER)),)


async def get_structured_content(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-structured-content``: fixed weather per city, as text and structured content."""
    del ctx
    parsed, issues = zodlike.check(WEATHER_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("get-structured-content", issues)
    weather = WEATHER[str(parsed["location"])]
    return {"content": [text(jsvalues.stringify(weather))], "structuredContent": dict(weather)}


# -- dynamic resources -----------------------------------------------------------------------------

LINKS_ARGS: Final = (Arg("count", "number", default=3, minimum=1, maximum=10),)


async def get_resource_links(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-resource-links``: odd ids link blob resources, even ids text resources."""
    del ctx
    parsed, issues = zodlike.check(LINKS_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("get-resource-links", issues)
    count = jsvalues.to_double(parsed["count"])  # type: ignore[arg-type]
    content: list[JsonValue] = [
        text(
            f"Here are {jsvalues.number_text(count)} resource links to resources available "
            "in this server:"
        )
    ]
    resource_id = 1
    while resource_id <= count:
        even = resource_id % 2 == 0
        content.append(
            {
                "name": f"{'Text' if even else 'Blob'} Resource {resource_id}",
                "uri": sessions.text_uri(resource_id) if even else sessions.blob_uri(resource_id),
                "description": f"Resource {resource_id}: plaintext resource",
                "mimeType": "text/plain",
                "type": "resource_link",
            }
        )
        resource_id += 1
    return {"content": content}


REFERENCE_ARGS: Final = (
    Arg("resourceType", "enum", default="Text", options=("Text", "Blob")),
    Arg("resourceId", "number", default=1),
)


async def get_resource_reference(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-resource-reference``: an embedded dynamic resource (created "now")."""
    parsed, issues = zodlike.check(REFERENCE_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("get-resource-reference", issues)
    resource_id = jsvalues.to_double(parsed["resourceId"])  # type: ignore[arg-type]
    if not jsvalues.is_positive_integer(resource_id):
        return zodlike.tool_error(
            f"Invalid resourceId: {jsvalues.number_text(resource_id)}. "
            "Must be a finite positive integer."
        )
    resource = (
        sessions.text_resource(sessions.text_uri(resource_id), resource_id, ctx.now_ns)
        if parsed["resourceType"] == "Text"
        else sessions.blob_resource(sessions.blob_uri(resource_id), resource_id, ctx.now_ns)
    )
    shown = jsvalues.number_text(resource_id)
    return {
        "content": [
            text(f"Returning resource reference for Resource {shown}:"),
            {"type": "resource", "resource": resource},
            text(f"You can access this resource using the URI: {resource['uri']}"),
        ]
    }


# -- gzip and session resources -------------------------------------------------------------------

GZIP_ARGS: Final = (
    Arg("name", default="README.md.gz"),
    Arg("data", "url", default=README_URL),
    Arg("outputType", "enum", default="resourceLink", options=("resourceLink", "resource")),
)


def node_gzip(data: bytes) -> bytes:
    """``zlib.gzipSync(data)`` byte for byte: level 6 deflate, macOS gzip header."""
    compressor = zlib.compressobj(-1, zlib.DEFLATED, -15, 8, zlib.Z_DEFAULT_STRATEGY)
    deflated = compressor.compress(data) + compressor.flush()
    header = bytes([0x1F, 0x8B, 8, 0, 0, 0, 0, 0, 0, GZIP_OS_MACOS])
    return header + deflated + struct.pack("<II", zlib.crc32(data), len(data) & 0xFFFFFFFF)


def recorded_download(ctx: RequestContext, href: str) -> tuple[bytes, float] | None:
    """What the real server fetched from ``href`` when recorded, and how long the call took.

    ``None``: never recorded.

    The fake never reaches the network: an unrecorded http(s) URL fails as an unreachable one
    does on the real server (``fetch failed``, 000172).
    """
    for exchange in ctx.runtime.recordings.for_endpoint("MCP tools/call:gzip-file-as-resource"):
        sent = exchange.request.body.parsed()
        reply = exchange.response.body.parsed() if exchange.response else None
        params = sent.get("params") if isinstance(sent, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if not isinstance(arguments, dict) or not isinstance(reply, dict):
            continue
        fetched = urls.parse(str(arguments.get("data", README_URL)))
        result = reply.get("result")
        content = result.get("content") if isinstance(result, dict) else None
        block = content[0] if isinstance(content, list) and content else None
        resource = block.get("resource") if isinstance(block, dict) else None
        blob = resource.get("blob") if isinstance(resource, dict) else None
        if fetched is not None and fetched.href == href and isinstance(blob, str):
            return gzip_module.decompress(base64.b64decode(blob)), exchange.elapsed_ms
    return None


def register_session_resource(ctx: RequestContext, uri: str, name: str, blob: str) -> None:
    """``registerSessionResource``: replace a resource of the same URI, announce the change."""
    resources: dict[str, JsonObject] = sessions.of(ctx).setdefault("session_resources", {})
    if uri in resources:
        del resources[uri]
        ctx.push("notifications/resources/list_changed")
    resources[uri] = {"uri": uri, "name": name, "mimeType": "application/gzip", "blob": blob}
    ctx.push("notifications/resources/list_changed")


async def gzip_file_as_resource(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``gzip-file-as-resource``: compress a data: URL (or the recorded README) into a resource."""
    parsed, issues = zodlike.check(GZIP_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("gzip-file-as-resource", issues)
    name, source, output = str(parsed["name"]), str(parsed["data"]), parsed["outputType"]
    url = urls.parse(source)
    if url is None or url.scheme not in {"http", "https", "data"}:
        return zodlike.tool_error(
            f"Error processing file {source}: Unsupported URL protocol for {source}. "
            "Only http, https, and data URLs are supported."
        )
    if url.scheme == "data":
        content = urls.data_url_bytes(url)
    else:
        download = recorded_download(ctx, url.href)
        content = download[0] if download is not None else None
        if download is not None:  # the fetch took the recorded time (latency covers the rest)
            await ctx.pause(max(download[1] - GZIP_OVERHEAD_MS, 0) / 1000)
    if content is None:
        return zodlike.tool_error("fetch failed")
    blob = base64.b64encode(node_gzip(content)).decode()
    uri = f"demo://resource/session/{name}"
    register_session_resource(ctx, uri, name, blob)
    if output == "resource":
        resource: JsonObject = {"uri": uri, "mimeType": "application/gzip", "blob": blob}
        return {"content": [{"type": "resource", "resource": resource}]}
    link: JsonObject = {
        "name": name,
        "uri": uri,
        "mimeType": "application/gzip",
        "type": "resource_link",
    }
    return {"content": [link]}


async def list_resources(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/list``: the documents, then this session's gzip resources (newest last)."""
    del params
    listed = [
        {"uri": item["uri"], "name": item["name"], "mimeType": item["mimeType"]}
        for item in sessions.of(ctx).get("session_resources", {}).values()
    ]
    return {"resources": [*ctx.model.mcp.resources, *listed]}  # type: ignore[union-attr]


def static_document(ctx: RequestContext, href: str) -> JsonValue:
    """A document resource, its text as recorded (``None``: not a document)."""
    for listed in ctx.model.mcp.resources:  # type: ignore[union-attr]
        if not isinstance(listed, dict) or listed.get("uri") != href:
            continue
        endpoint = f"MCP resources/read:{href}"
        for exchange in ctx.runtime.recordings.for_endpoint(endpoint):
            reply = exchange.response.body.parsed() if exchange.response else None
            result = reply.get("result") if isinstance(reply, dict) else None
            if isinstance(result, dict):
                return result
    return None


_TEMPLATES: Final = (
    ("text", re.compile(r"demo://resource/dynamic/text/([^/,]+)")),
    ("blob", re.compile(r"demo://resource/dynamic/blob/([^/,]+)")),
)


async def read_resource(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/read``: documents, session resources, then the two dynamic templates."""
    url = urls.parse(zodlike.require_string(params, "uri"))
    if url is None:
        raise RpcError(zodlike.INTERNAL_ERROR, "Invalid URL")
    href = url.href
    document = static_document(ctx, href)
    if document is not None:
        return document
    kept = sessions.of(ctx).get("session_resources", {}).get(href)
    if kept is not None:
        return {
            "contents": [{"uri": kept["uri"], "mimeType": kept["mimeType"], "blob": kept["blob"]}]
        }
    for kind, pattern in _TEMPLATES:
        match = pattern.fullmatch(href)
        if match is None:
            continue
        resource_id = jsvalues.number_of(match.group(1))
        if not jsvalues.is_positive_integer(resource_id):
            raise RpcError(zodlike.INTERNAL_ERROR, f"Unknown resource: {href}")
        make = sessions.text_resource if kind == "text" else sessions.blob_resource
        return {"contents": [make(href, resource_id, ctx.now_ns)]}
    raise RpcError(zodlike.INVALID_PARAMS, f"MCP error -32602: Resource {href} not found")


# -- logging and subscriptions --------------------------------------------------------------------

LOG_MESSAGES: Final = (
    ("debug", "Debug-level message"),
    ("info", "Info-level message"),
    ("notice", "Notice-level message"),
    ("warning", "Warning-level message"),
    ("error", "Error-level message"),
    ("critical", "Critical-level message"),
    ("alert", "Alert level-message"),
    ("emergency", "Emergency-level message"),
)
LOG_PACE_S: Final = 5.0


async def set_level(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``logging/setLevel``: later log messages below it are not sent."""
    level = params.get("level")
    if level not in sessions.LEVELS:
        choices = "|".join(f'"{name}"' for name in sessions.LEVELS)
        raise zodlike.request_error(
            [
                {
                    "code": "invalid_value",
                    "values": list(sessions.LEVELS),
                    "path": ["params", "level"],
                    "message": f"Invalid option: expected one of {choices}",
                }
            ]
        )
    sessions.of(ctx)["log_level"] = level
    return {}


def _stop(state: dict[str, Any], key: str) -> bool:
    task: asyncio.Task[None] | None = state.pop(key, None)
    if task is None:
        return False
    task.cancel()
    return True


async def toggle_simulated_logging(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``toggle-simulated-logging``: a random-level log message now and every 5 s, or stop."""
    del arguments
    state = sessions.of(ctx)
    session = ctx.mcp_session
    if _stop(state, "logging_task"):
        return {"content": [text(f"Stopped simulated logging for session {session}")]}
    suffix = f" - SessionId {session}" if session else ""
    rng = ctx.rng

    async def emit(channel: Channel) -> None:
        while True:
            level, message = rng.choice(LOG_MESSAGES)
            sessions.log(channel, state, level, message + suffix)
            await channel.pause(LOG_PACE_S)

    state["logging_task"] = ctx.background(emit)
    return {
        "content": [
            text(
                f"Started simulated, random-leveled logging for session {session} at a 5 second "
                "pace. Client's selected logging level will be respected. If an interval elapses "
                "and the message to be sent is below the selected level, it will not be sent. "
                "Thus at higher chosen logging levels, messages should arrive further apart. "
            )
        ]
    }


async def subscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/subscribe``: acknowledged with a log message; any URI is accepted."""
    uri = zodlike.require_string(params, "uri")
    state = sessions.of(ctx)
    sessions.log(
        ctx.channel,
        state,
        "info",
        f"Received Subscribe Resource request for URI: {uri} from session {ctx.mcp_session}",
    )
    state.setdefault("subscriptions", {})[uri] = True
    return {}


async def unsubscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/unsubscribe``: acknowledged with a log message, even if never subscribed."""
    uri = zodlike.require_string(params, "uri")
    state = sessions.of(ctx)
    sessions.log(
        ctx.channel,
        state,
        "info",
        f"Received Unsubscribe Resource request: {uri} from session {ctx.mcp_session}",
    )
    subscriptions: dict[str, bool] = state.setdefault("subscriptions", {})
    if uri in subscriptions:
        subscriptions[uri] = False
    return {}


async def toggle_subscriber_updates(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``toggle-subscriber-updates``: ``resources/updated`` for every subscription each 5 s."""
    del arguments
    state = sessions.of(ctx)
    session = ctx.mcp_session
    if _stop(state, "updates_task"):
        return {"content": [text(f"Stopped simulated resource updates for session {session}")]}

    async def updates(channel: Channel) -> None:
        while True:
            for uri, subscribed in list(state.get("subscriptions", {}).items()):
                if subscribed:
                    channel.push("notifications/resources/updated", {"uri": uri})
            await channel.pause(LOG_PACE_S)

    state["updates_task"] = ctx.background(updates)
    return {
        "content": [
            text(
                f"Started simulated resource updated notifications for session {session} at a 5 "
                "second pace. Client will receive updates for any resources the it is subscribed "
                "to."
            )
        ]
    }


# -- long-running operation -----------------------------------------------------------------------

LONG_ARGS: Final = (Arg("duration", "number", default=10), Arg("steps", "number", default=5))


def _progress_token(ctx: RequestContext) -> JsonValue:
    message = ctx.json_body if isinstance(ctx.json_body, dict) else {}
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta.get("progressToken") if isinstance(meta, dict) else None


async def _clock_pause(ctx: RequestContext, seconds: float) -> None:
    """Wait on the session's clock (advancing it via ``/__fauxcall/clock`` ends the wait)."""
    if seconds <= 0:
        return
    if ctx.channel is None:  # pragma: no cover - MCP requests always have one
        await ctx.pause(seconds)
        return
    await ctx.channel.pause(seconds)


def step_seconds(duration: float, steps: float) -> float:
    """How long ``setTimeout(resolve, stepDuration * 1000)`` really waits."""
    milliseconds = duration / steps * 1000 if steps else math.inf
    if math.isnan(milliseconds) or milliseconds < 1 or milliseconds > MAX_TIMER_MS:
        return 0.001  # Node waits at least 1 ms (000534: 200 steps of 0 ms took 277 ms)
    return milliseconds / 1000


async def trigger_long_running_operation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-long-running-operation``: steps over ``duration`` with progress notifications."""
    parsed, issues = zodlike.check(LONG_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("trigger-long-running-operation", issues)
    duration = jsvalues.to_double(parsed["duration"])  # type: ignore[arg-type]
    steps = jsvalues.to_double(parsed["steps"])  # type: ignore[arg-type]
    token = _progress_token(ctx)
    wait = step_seconds(duration, steps)
    step = 1
    while step < steps + 1:
        # the session's clock, even in fast mode: the steps are what clients wait for and cancel
        await _clock_pause(ctx, wait)
        if token is not None:  # the real server keeps reporting after a cancel (on_cancel finish)
            update = {"progress": step, "total": jsvalues.plain(steps), "progressToken": token}
            ctx.notify("notifications/progress", update)
        step += 1
    shown = jsvalues.number_text
    return {
        "content": [
            text(
                f"Long running operation completed. Duration: {shown(duration)} seconds, "
                f"Steps: {shown(steps)}."
            )
        ]
    }


# -- requests to the client -----------------------------------------------------------------------

SAMPLING_ARGS: Final = (Arg("prompt"), Arg("maxTokens", "number", default=100))
SYSTEM_PROMPT: Final = "You are a helpful test server."


def _sampling_request(tool: str, parsed: JsonObject, *, task: bool) -> JsonObject:
    request: JsonObject = {"task": {"ttl": 300000}} if task else {}
    request["messages"] = [
        {"role": "user", "content": text(f"Resource {tool} context: {parsed['prompt']}")}
    ]
    request["systemPrompt"] = SYSTEM_PROMPT
    request["maxTokens"] = jsvalues.plain(parsed["maxTokens"])
    request["temperature"] = 0.7
    return request


async def trigger_sampling_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-sampling-request``: asks the client's LLM, shows its answer."""
    parsed, issues = zodlike.check(SAMPLING_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("trigger-sampling-request", issues)
    request = _sampling_request("trigger-sampling-request", parsed, task=False)
    try:
        answer = await ctx.ask_client("sampling/createMessage", request)
    except ClientError as error:
        return _client_failure(error)
    result, problems = sessions.sampling_result(answer)
    if problems:
        return _issues_text(problems)
    shown = jsvalues.stringify(result, indent=2)
    return {"content": [text(f"LLM sampling result: \n{shown}")]}


def _user_inputs(content: JsonObject) -> str:
    """The lines ``trigger-elicitation-request`` shows for the fields it knows."""
    template, truthy = jsvalues.template, jsvalues.truthy
    lines: list[str] = []
    if truthy(content.get("name")):
        lines.append(f"- Name: {template(content['name'])}")
    if "check" in content:
        lines.append(f"- Agreed to terms: {template(content['check'])}")
    for key, label in (("color", "Favorite Color"), ("email", "Email"), ("homepage", "Homepage")):
        if truthy(content.get(key)):
            lines.append(f"- {label}: {template(content[key])}")
    if truthy(content.get("birthdate")):
        lines.append(f"- Birthdate: {template(content['birthdate'])}")
    if "integer" in content:
        lines.append(f"- Favorite Integer: {template(content['integer'])}")
    if "number" in content:
        lines.append(f"- Favorite Number: {template(content['number'])}")
    if truthy(content.get("petType")):
        lines.append(f"- Pet Type: {template(content['petType'])}")
    return "User inputs:\n" + "\n".join(lines)


async def trigger_elicitation_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-elicitation-request``: asks the user for a form, shows what came back."""
    del arguments
    try:
        answer = await ctx.ask_client(
            "elicitation/create", ELICITATION_PARAMS, within=ELICITATION_TIMEOUT_S
        )
    except ClientError as error:
        return _client_failure(error)
    result, problems = sessions.elicit_result(answer)
    if problems:
        return _issues_text(problems)
    content: list[JsonValue] = []
    data = result.get("content")
    if result["action"] == "accept" and isinstance(data, dict):
        content.append(text("✅ User provided the requested information!"))
        content.append(text(_user_inputs(data)))
    elif result["action"] == "decline":
        content.append(text("❌ User declined to provide the requested information."))
    elif result["action"] == "cancel":
        content.append(text("⚠️ User cancelled the elicitation dialog."))
    content.append(text(f"\nRaw result: {jsvalues.stringify(result, indent=2)}"))
    return {"content": content}


URL_ARGS: Final = (
    Arg("url", "url"),
    Arg("message", default="Please open the link to complete this action."),
    Arg("elicitationId", optional=True),
    Arg("errorPath", "boolean", default=False),
)


async def trigger_url_elicitation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-url-elicitation``: a URL elicitation, by request or by the -32042 error path."""
    parsed, issues = zodlike.check(URL_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("trigger-url-elicitation", issues)
    url, message = str(parsed["url"]), str(parsed["message"])
    requested = parsed.get("elicitationId")
    elicitation_id = str(requested) if requested is not None else _uuid(ctx)
    if parsed["errorPath"]:
        issued: set[str] = sessions.of(ctx).setdefault("error_path_elicitations", set())
        key = f"{ctx.mcp_session or 'default'}\0{url}\0{requested if requested is not None else ''}"
        if key in issued:  # the retry proceeds, so the client doesn't loop on the error
            issued.discard(key)
        else:
            issued.add(key)
            prerequisite: JsonObject = {
                "mode": "url",
                "url": "https://modelcontextprotocol.io",
                "message": "Open this link to satisfy the prerequisite, then retry the request.",
                "elicitationId": _uuid(ctx),
            }
            raise RpcError(
                -32042,
                "MCP error -32042: This request requires browser-based authorization.",
                {"elicitations": [prerequisite]},
            )
    request: JsonObject = {
        "mode": "url",
        "url": url,
        "message": message,
        "elicitationId": elicitation_id,
    }
    try:
        answer = await ctx.ask_client("elicitation/create", request, within=ELICITATION_TIMEOUT_S)
    except ClientError as error:
        return _client_failure(error)
    result, problems = sessions.elicit_result(answer)
    if problems:
        return _issues_text(problems)
    content: list[JsonValue] = []
    if result["action"] == "accept":
        content.append(
            text(
                f"✅ User completed the URL elicitation flow.\nElicitation ID: {elicitation_id}"
                f"\nURL: {url}"
            )
        )
    elif result["action"] == "decline":
        content.append(
            text(f"❌ User declined to open the URL (Elicitation ID: {elicitation_id}).")
        )
    else:
        content.append(
            text(f"⚠️ User cancelled the URL elicitation (Elicitation ID: {elicitation_id}).")
        )
    content.append(text(f"\nRaw result: {jsvalues.stringify(result, indent=2)}"))
    return {"content": content}


# -- roots ---------------------------------------------------------------------------------------

CLIENT_TIMEOUT_S: Final = 60.0
"""The SDK's default timeout for a request the server sends the client."""
ROOTS_DELAY_S: Final = 0.35
"""The server asks for roots 350 ms after ``notifications/initialized`` (on the GET stream)."""
NO_ROOTS: Final = (
    "The client supports roots but no roots are currently configured.\n\n"
    "This could mean:\n"
    "1. The client hasn't provided any roots yet\n"
    "2. The client provided an empty roots list\n"
    "3. The roots configuration is still being loaded"
)


def _valid_roots(answer: JsonValue) -> list[JsonObject] | None:
    """``ListRootsResultSchema``: roots whose URIs start with ``file://`` (else it throws)."""
    roots = answer.get("roots") if isinstance(answer, dict) else None
    if not isinstance(roots, list):
        return None
    parsed: list[JsonObject] = []
    for root in roots:
        uri = root.get("uri") if isinstance(root, dict) else None
        if not isinstance(uri, str) or not uri.startswith("file://"):
            return None
        name = root.get("name")  # type: ignore[union-attr]
        if name is not None and not isinstance(name, str):
            return None
        parsed.append(root)  # type: ignore[arg-type]
    return parsed


def stream_check(ctx: RequestContext) -> Callable[[], bool]:
    """Whether this client has its GET stream open (where ``listRoots()`` goes)."""
    fake = ctx.runtime.mcp
    session = ctx.mcp_session or ""
    return lambda: fake is not None and session in fake.streams


async def request_roots(
    channel: Channel | None, state: dict[str, Any], is_open: Callable[[], bool]
) -> None:
    """``listRoots()`` on the GET stream; store them and log, or fail quietly (console only).

    With no GET stream open the SDK drops the request (``send`` discards what no stream can
    carry) and waits for an answer that never comes: the call fails after the 60 s timeout.
    """
    if channel is None:  # pragma: no cover - MCP requests always have one
        return
    reachable = is_open()
    try:
        answer = await channel.ask("roots/list")
    except ClientError:
        if not reachable:
            await channel.pause(CLIENT_TIMEOUT_S)
        return
    roots = _valid_roots(answer)
    if roots is None:
        return
    state["roots"] = roots
    sessions.log(
        channel,
        state,
        "info",
        f"Roots updated: {len(roots)} root(s) received from client",
        logger="everything-server",
    )


async def sync_roots(
    channel: Channel | None, state: dict[str, Any], is_open: Callable[[], bool]
) -> list[JsonObject] | None:
    """``syncRoots``: the session's roots, asking the client while it has none."""
    if "roots" not in state:
        state["roots_listener"] = True
        await request_roots(channel, state, is_open)
    roots: list[JsonObject] | None = state.get("roots")
    return roots


async def get_roots_list(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-roots-list``: the client's roots (asked once, then remembered)."""
    del arguments
    roots = await sync_roots(ctx.channel, sessions.of(ctx), stream_check(ctx))
    if not roots:
        return {"content": [text(NO_ROOTS)]}
    listing = "\n\n".join(
        f"{index}. {root['name'] if jsvalues.truthy(root.get('name')) else 'Unnamed Root'}"
        f"\n   URI: {root['uri']}"
        for index, root in enumerate(roots, start=1)
    )
    return {
        "content": [
            text(
                f"Current MCP Roots ({len(roots)} total):\n\n{listing}\n\n"
                "Note: This server demonstrates the roots protocol capability but doesn't "
                "actually access files. The roots are provided by the MCP client and can be used "
                "by servers that need file system access."
            )
        ]
    }


async def initialized(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``notifications/initialized``: conditional tools appear; roots are asked for at 350 ms."""
    del params
    state = sessions.of(ctx)
    state["initialized"] = True
    if "roots" in ctx.client_capabilities:
        is_open = stream_check(ctx)

        async def first_sync(channel: Channel) -> None:
            await channel.pause(ROOTS_DELAY_S)
            await sync_roots(channel, state, is_open)

        ctx.background(first_sync)
    return None


async def roots_changed(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``notifications/roots/list_changed``: the server asks for the roots again."""
    del params
    state = sessions.of(ctx)
    if state.get("roots_listener"):
        is_open = stream_check(ctx)

        async def refresh(channel: Channel) -> None:
            await request_roots(channel, state, is_open)

        ctx.background(refresh)
    return None


# -- asynchronous (client task) requests ---------------------------------------------------------

Field = tuple[str, str, bool]
"""A field of a zod object the server parses an answer with: name, kind, optional."""
TASK_FIELDS: Final[tuple[Field, ...]] = (
    ("taskId", "string", False),
    ("status", "string", False),
    ("pollInterval", "number", True),
    ("statusMessage", "string", True),
)
SAMPLING_FIELDS: Final[tuple[Field, ...]] = (
    ("role", "string", False),
    ("content", "any", False),
    ("model", "string", False),
    ("stopReason", "string", True),
)
ELICITATION_FIELDS: Final[tuple[Field, ...]] = (
    ("action", "string", False),
    ("content", "any", True),
)
POLL_FIELDS: Final[tuple[Field, ...]] = (
    ("status", "string", False),
    ("statusMessage", "string", True),
)


class _Stopped(Exception):  # noqa: N818 - control flow, carries the tool's answer
    """The async tool ends early: an answer the SDK refused, or the client cancelled the call."""

    def __init__(self, answer: JsonObject) -> None:
        super().__init__("stopped")
        self.answer = answer


def _fits(value: JsonValue, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return True


def _object(
    value: JsonValue, fields: tuple[Field, ...], path: list[str | int]
) -> tuple[JsonObject, list[JsonObject]]:
    """A zod object's parse: its known fields in schema order, and its issues."""
    if not isinstance(value, dict):
        return {}, [zodlike.type_issue(path, "object", value, present=True)]
    parsed: JsonObject = {}
    issues: list[JsonObject] = []
    for name, kind, optional in fields:
        present = name in value
        if not present and optional:
            continue
        if kind == "any" and not present:
            where: list[JsonValue] = [*path, name]
            issues.append(
                {
                    "code": "invalid_type",
                    "expected": "nonoptional",
                    "path": where,
                    "message": "Invalid input: expected nonoptional, received undefined",
                }
            )
        elif not present or not _fits(value[name], kind):
            issues.append(zodlike.type_issue([*path, name], kind, value.get(name), present=present))
        else:
            parsed[name] = value[name]
    return parsed, issues


def _task_or_direct(
    answer: JsonValue, direct: tuple[Field, ...]
) -> tuple[JsonObject | None, JsonObject]:
    """The SDK's ``z.union([{task}, direct])`` parse of a client's answer: (task, direct)."""
    parsed_task: JsonObject = {}
    task_issues: list[JsonValue] = []
    if not isinstance(answer, dict):
        task_issues.append(zodlike.type_issue([], "object", answer, present=True))
    elif "task" not in answer:
        task_issues.append(zodlike.type_issue(["task"], "object", None, present=False))
    else:
        parsed_task, problems = _object(answer["task"], TASK_FIELDS, ["task"])
        task_issues.extend(problems)
    if not task_issues:
        return parsed_task, {}
    parsed, problems = _object(answer, direct, [])
    if not problems:
        return None, parsed
    direct_issues: list[JsonValue] = [*problems]
    union: JsonObject = {
        "code": "invalid_union",
        "errors": [task_issues, direct_issues],
        "path": [],
        "message": "Invalid input",
    }
    raise _Stopped(_issues_text([union]))


async def _ask_unless_cancelled(ctx: RequestContext, method: str, params: JsonObject) -> JsonValue:
    """``extra.sendRequest``: refused once the client cancelled the call (its abort signal)."""
    if ctx.cancelled:
        raise _Stopped(zodlike.tool_error("Request was cancelled"))
    return await ctx.ask_client(method, params)


async def _poll_client_task(
    ctx: RequestContext, task: JsonObject, *, limit: int, every_poll: bool
) -> tuple[str, str | None, list[str], int]:
    """Poll the client's ``tasks/get`` once a second until the task ends (or ``limit`` polls)."""
    task_id = str(task["taskId"])
    status = str(task["status"])
    message: str | None = None
    progress = [f"Task created: {task_id}"]
    attempts = 0
    while status not in _TERMINAL and attempts < limit:
        await _clock_pause(ctx, TASK_POLL_S)
        attempts += 1
        poll = await _ask_unless_cancelled(ctx, "tasks/get", {"taskId": task_id})
        parsed, issues = _object(poll, POLL_FIELDS, [])
        if issues:  # z.looseObject({status: z.string(), statusMessage: z.string().optional()})
            raise _Stopped(_issues_text(issues))
        status = str(parsed["status"])
        found = parsed.get("statusMessage")
        message = found if isinstance(found, str) else None
        if every_poll or attempts == 1 or attempts % 10 == 0 or status != "input_required":
            progress.append(f"Poll {attempts}: {status}{f' - {message}' if message else ''}")
    return status, message, progress, attempts


def _unfinished(
    status: str, message: str | None, progress: list[str], attempts: int, limit: int
) -> JsonObject | None:
    """The answer for a task that timed out, failed or was cancelled (``None``: it completed)."""
    joined = "\n".join(progress)
    if attempts >= limit:
        return {
            "content": [
                text(f"[TIMEOUT] Task timed out after {limit} poll attempts\n\nProgress:\n{joined}")
            ]
        }
    if status in {"failed", "cancelled"}:
        return {
            "content": [
                text(f"[{status.upper()}] {message or 'No message'}\n\nProgress:\n{joined}")
            ]
        }
    return None


async def _client_task(  # noqa: PLR0913 - the two tools differ in each
    ctx: RequestContext,
    method: str,
    request: JsonObject,
    direct: tuple[Field, ...],
    limit: int,
    *,
    every_poll: bool,
) -> tuple[JsonObject, list[str], JsonValue]:
    """Ask the client, poll its task, fetch its result: (direct answer or {}, progress, result)."""
    answer = await ctx.ask_client(method, request)
    task, parsed = _task_or_direct(answer, direct)
    if task is None:
        sync = jsvalues.stringify(parsed, indent=2)
        raise _Stopped({"content": [text(f"[SYNC] Client executed synchronously:\n{sync}")]})
    status, message, progress, attempts = await _poll_client_task(
        ctx, task, limit=limit, every_poll=every_poll
    )
    unfinished = _unfinished(status, message, progress, attempts, limit)
    if unfinished is not None:
        raise _Stopped(unfinished)
    result = await _ask_unless_cancelled(ctx, "tasks/result", {"taskId": task["taskId"]})
    return parsed, progress, result


async def trigger_sampling_request_async(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-sampling-request-async``: sampling the client may run as a task (polled)."""
    parsed, issues = zodlike.check(SAMPLING_ARGS, arguments)
    if issues:
        return zodlike.invalid_tool_arguments("trigger-sampling-request-async", issues)
    request = _sampling_request("trigger-sampling-request-async", parsed, task=True)
    try:
        _, progress, result = await _client_task(
            ctx, "sampling/createMessage", request, SAMPLING_FIELDS, SAMPLING_POLLS, every_poll=True
        )
    except ClientError as error:
        return _client_failure(error)
    except _Stopped as stopped:
        return stopped.answer
    joined = "\n".join(progress)
    return {
        "content": [
            text(
                f"[COMPLETED] Async sampling completed!\n\n**Progress:**\n{joined}\n\n"
                f"**Result:**\n{jsvalues.stringify(result, indent=2)}"
            )
        ]
    }


async def trigger_elicitation_request_async(
    ctx: RequestContext, arguments: JsonObject
) -> JsonValue:
    """``trigger-elicitation-request-async``: elicitation the client may run as a task."""
    del arguments
    try:
        _, progress, result = await _client_task(
            ctx,
            "elicitation/create",
            ASYNC_ELICITATION_PARAMS,
            ELICITATION_FIELDS,
            ELICITATION_POLLS,
            every_poll=False,
        )
    except ClientError as error:
        return _client_failure(error)
    except _Stopped as stopped:
        return stopped.answer
    outcome = result if isinstance(result, dict) else {}
    content: list[JsonValue] = []
    data = outcome.get("content")
    if outcome.get("action") == "accept" and jsvalues.truthy(data) and isinstance(data, dict):
        content.append(text("[COMPLETED] User provided the requested information!"))
        lines: list[str] = []
        if jsvalues.truthy(data.get("name")):
            lines.append(f"- Name: {jsvalues.template(data['name'])}")
        if jsvalues.truthy(data.get("favoriteColor")):
            lines.append(f"- Favorite Color: {jsvalues.template(data['favoriteColor'])}")
        if "agreeToTerms" in data:
            lines.append(f"- Agreed to terms: {jsvalues.template(data['agreeToTerms'])}")
        content.append(text("User inputs:\n" + "\n".join(lines)))
    elif outcome.get("action") == "decline":
        content.append(text("[DECLINED] User declined to provide the requested information."))
    elif outcome.get("action") == "cancel":
        content.append(text("[CANCELLED] User cancelled the elicitation dialog."))
    joined = "\n".join(progress)
    content.append(
        text(f"\nProgress:\n{joined}\n\nRaw result: {jsvalues.stringify(result, indent=2)}")
    )
    return {"content": content}


# -- prompts and completions ----------------------------------------------------------------------

PROMPT_ARGS: Final[dict[str, tuple[Arg, ...] | None]] = {
    "simple-prompt": None,
    "args-prompt": (Arg("city"), Arg("state", optional=True)),
    "completable-prompt": (Arg("department"), Arg("name")),
    "resource-prompt": (Arg("resourceType"), Arg("resourceId")),
}


def _user(content: JsonObject) -> JsonObject:
    return {"role": "user", "content": content}


def _prompt_messages(ctx: RequestContext, name: str, parsed: JsonObject) -> list[JsonValue]:
    match name:
        case "simple-prompt":
            return [_user(text("This is a simple prompt without arguments."))]
        case "args-prompt":
            state = parsed.get("state")
            place = f"{parsed['city']}{f', {state}' if jsvalues.truthy(state) else ''}"
            return [_user(text(f"What's weather in {place}?"))]
        case "completable-prompt":
            return [
                _user(
                    text(
                        f"Please promote {parsed['name']} to the head of the "
                        f"{parsed['department']} team."
                    )
                )
            ]
    kind, raw_id = str(parsed["resourceType"]), str(parsed["resourceId"])
    if kind not in {"Text", "Blob"}:
        raise RpcError(
            zodlike.INTERNAL_ERROR, f"Invalid resourceType: {kind}. Must be Text or Blob."
        )
    resource_id = jsvalues.number_of(raw_id)
    if not jsvalues.is_positive_integer(resource_id):
        raise RpcError(
            zodlike.INTERNAL_ERROR,
            f"Invalid resourceId: {raw_id}. Must be a finite positive integer.",
        )
    resource = (
        sessions.text_resource(sessions.text_uri(resource_id), resource_id, ctx.now_ns)
        if kind == "Text"
        else sessions.blob_resource(sessions.blob_uri(resource_id), resource_id, ctx.now_ns)
    )
    shown = jsvalues.number_text(resource_id)
    return [
        _user(
            text(
                f"This prompt includes the {kind} resource with id: {shown}. "
                "Please analyze the following resource:"
            )
        ),
        _user({"type": "resource", "resource": resource}),
    ]


async def get_prompt(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``prompts/get``."""
    issues: list[JsonObject] = []
    name = params.get("name")
    if not isinstance(name, str):
        issues.append(
            zodlike.type_issue(["params", "name"], "string", name, present="name" in params)
        )
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        issues.append(
            zodlike.type_issue(["params", "arguments"], "record", arguments, present=True)
        )
        arguments = {}
    for key, value in arguments.items():
        if not isinstance(value, str):
            issues.append(
                zodlike.type_issue(["params", "arguments", key], "string", value, present=True)
            )
    if issues:
        raise zodlike.request_error(issues)
    if name not in PROMPT_ARGS:
        raise RpcError(zodlike.INVALID_PARAMS, f"MCP error -32602: Prompt {name} not found")
    schema = PROMPT_ARGS[str(name)]
    parsed: JsonObject = {}
    if schema is not None:
        parsed, problems = zodlike.check(schema, arguments)
        if problems:
            raise zodlike.invalid_prompt_arguments(str(name), problems)
    return {"messages": _prompt_messages(ctx, str(name), parsed)}


TEAMS: Final = {
    "Engineering": ("Alice", "Bob", "Charlie"),
    "Sales": ("David", "Eve", "Frank"),
    "Marketing": ("Grace", "Henry", "Iris"),
    "Support": ("John", "Kim", "Lee"),
}
TEMPLATES: Final = (
    "demo://resource/dynamic/text/{resourceId}",
    "demo://resource/dynamic/blob/{resourceId}",
)
MAX_COMPLETIONS: Final = 100


def _positive_id(value: str) -> list[str]:
    return [value] if jsvalues.is_positive_integer(jsvalues.number_of(value)) else []


def _completer(ref: JsonObject, argument: str) -> Callable[[str, JsonObject], list[str]] | None:
    """The completion callback for one argument (``None``: the argument has none)."""
    if ref["type"] == "ref/resource":
        return (lambda value, _context: _positive_id(value)) if argument == "resourceId" else None
    completers: dict[tuple[str, str], Callable[[str, JsonObject], list[str]]] = {
        ("completable-prompt", "department"): lambda value, _context: [
            department for department in TEAMS if department.startswith(value)
        ],
        ("completable-prompt", "name"): lambda value, context: [
            person
            for person in TEAMS.get(str(context.get("department")), ())
            if person.startswith(value)
        ],
        ("resource-prompt", "resourceType"): lambda value, _context: [
            kind for kind in ("Text", "Blob") if kind.startswith(value)
        ],
        ("resource-prompt", "resourceId"): lambda value, _context: _positive_id(value),
    }
    return completers.get((str(ref["name"]), argument))


def _ref_issues(ref: JsonValue, *, present: bool) -> list[list[JsonObject]]:
    """Zod's union branches for ``params.ref`` (an empty list: one branch passes)."""
    if not isinstance(ref, dict):
        missing = zodlike.type_issue([], "object", ref, present=present)
        return [[missing], [dict(missing)]]
    branches: list[list[JsonObject]] = []
    for kind, field in (("ref/prompt", "name"), ("ref/resource", "uri")):
        branch: list[JsonObject] = []
        if ref.get("type") != kind:
            branch.append(
                {
                    "code": "invalid_value",
                    "values": [kind],
                    "path": ["type"],
                    "message": f'Invalid input: expected "{kind}"',
                }
            )
        if not isinstance(ref.get(field), str):
            branch.append(
                zodlike.type_issue([field], "string", ref.get(field), present=field in ref)
            )
        if not branch:
            return []
        branches.append(branch)
    return branches


async def complete(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``completion/complete`` for the prompts' and templates' completable arguments."""
    issues: list[JsonObject] = []
    ref = params.get("ref")
    branches: list[JsonValue] = []
    for branch in _ref_issues(ref, present="ref" in params):
        listed: list[JsonValue] = [*branch]
        branches.append(listed)
    if branches:
        issues.append(
            {
                "code": "invalid_union",
                "errors": branches,
                "path": ["params", "ref"],
                "message": "Invalid input",
            }
        )
    argument = params.get("argument")
    if not isinstance(argument, dict):
        issues.append(
            zodlike.type_issue(
                ["params", "argument"], "object", argument, present="argument" in params
            )
        )
        argument = {}
    else:
        issues.extend(
            zodlike.type_issue(
                ["params", "argument", field],
                "string",
                argument.get(field),
                present=field in argument,
            )
            for field in ("name", "value")
            if not isinstance(argument.get(field), str)
        )
    if issues:
        raise zodlike.request_error(issues)
    assert isinstance(ref, dict)  # noqa: S101 - checked above
    if ref["type"] == "ref/prompt" and ref["name"] not in PROMPT_ARGS:
        raise RpcError(zodlike.INVALID_PARAMS, f"MCP error -32602: Prompt {ref['name']} not found")
    if ref["type"] == "ref/resource" and ref["uri"] not in TEMPLATES:
        registered = {item.get("uri") for item in ctx.model.mcp.resources if isinstance(item, dict)}  # type: ignore[union-attr]
        registered |= set(sessions.of(ctx).get("session_resources", {}))
        if ref["uri"] in registered:  # a plain resource: nothing to complete
            return {"completion": {"values": [], "hasMore": False}}
        raise RpcError(
            zodlike.INVALID_PARAMS, f"MCP error -32602: Resource template {ref['uri']} not found"
        )
    completer = _completer(ref, str(argument["name"]))
    if completer is None:
        return {"completion": {"values": [], "hasMore": False}}
    context = params.get("context")
    known = context.get("arguments") if isinstance(context, dict) else None
    values = completer(str(argument["value"]), known if isinstance(known, dict) else {})
    shown: list[JsonValue] = [*values[:MAX_COMPLETIONS]]
    return {
        "completion": {
            "values": shown,
            "total": len(values),
            "hasMore": len(values) > MAX_COMPLETIONS,
        }
    }


# -- tools/call checks and registrations ----------------------------------------------------------


async def tools_call(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Checks before any tool runs: the SDK's request schema, and the ``task`` parameter.

    A ``task`` that isn't an object, a ``name`` that isn't a string or ``arguments`` that
    aren't an object are refused with zod's issues (000487, 000011, 000012);
    ``simulate-research-query`` (offered once the client sent ``notifications/initialized``)
    needs the call's ``task``, which tool handlers don't see. Any other tool called as a task
    runs (its side effects happen: 000485-000486) and is then refused, as its answer is no
    ``CreateTaskResult`` (000483, 000484). Everything else goes on to FauxCall (unknown tools,
    then ``TOOL_HANDLERS``).
    """
    issues: list[JsonObject] = []
    if "task" in params and not isinstance(params["task"], dict):
        issues.append(
            zodlike.type_issue(["params", "task"], "object", params["task"], present=True)
        )
    name = params.get("name")
    if not isinstance(name, str):
        issues.append(
            zodlike.type_issue(["params", "name"], "string", name, present="name" in params)
        )
    if "arguments" in params and not isinstance(params["arguments"], dict):
        issues.append(
            zodlike.type_issue(["params", "arguments"], "record", params["arguments"], present=True)
        )
    if issues:
        raise zodlike.request_error(issues)
    initialized = bool(sessions.of(ctx).get("initialized"))
    if name == research.TOOL and initialized:
        return await research.call(ctx, params)
    if "task" in params:  # unknown tools too: their "not found" result is no CreateTaskResult
        tool = ctx.model.mcp.tool(str(name)) if ctx.model.mcp is not None else None
        ready = tool is not None and tool.offered_to(ctx.client_capabilities)
        if tool is not None and ready and (initialized or not tool.requires):
            handler = TOOL_HANDLERS.get(str(name))
            arguments = params.get("arguments")
            if handler is not None:
                await handler(ctx, arguments if isinstance(arguments, dict) else {})
        raise research.invalid_task_result()
    return None


TOOL_HANDLERS.update(
    {
        "echo": echo,
        "get-annotated-message": get_annotated_message,
        "get-resource-links": get_resource_links,
        "get-resource-reference": get_resource_reference,
        "get-structured-content": get_structured_content,
        "get-sum": get_sum,
        "gzip-file-as-resource": gzip_file_as_resource,
        "toggle-simulated-logging": toggle_simulated_logging,
        "toggle-subscriber-updates": toggle_subscriber_updates,
        "trigger-long-running-operation": trigger_long_running_operation,
        "trigger-sampling-request": trigger_sampling_request,
        "trigger-elicitation-request": trigger_elicitation_request,
        "trigger-url-elicitation": trigger_url_elicitation,
        "get-roots-list": get_roots_list,
        "trigger-sampling-request-async": trigger_sampling_request_async,
        "trigger-elicitation-request-async": trigger_elicitation_request_async,
    }
)
METHOD_HANDLERS.update(
    {
        "tools/call": tools_call,
        "resources/list": list_resources,
        "resources/read": read_resource,
        "resources/subscribe": subscribe,
        "resources/unsubscribe": unsubscribe,
        "logging/setLevel": set_level,
        "prompts/get": get_prompt,
        "completion/complete": complete,
        "tasks/get": research.get,
        "tasks/list": research.list_tasks,
        "tasks/cancel": research.cancel,
        "tasks/result": research.result,
        "notifications/initialized": initialized,
        "notifications/roots/list_changed": roots_changed,
    }
)
