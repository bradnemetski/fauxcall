"""Custom behavior of this fake: yours to edit, ``fauxcall scaffold`` never overwrites it.

Every tool of the Everything server is ``computed`` here: its answers are small functions of the
arguments (validated with the server's exact Zod wording, see :mod:`zodlike`), the clock (dynamic
resources print the local time) and per-MCP-session state (session resources, toggles, tasks).
The other MCP methods the server implements with code (``resources/read``, ``prompts/get``,
``completion/complete``, ``logging/setLevel``, subscriptions and ``tasks/*``) are answered in
``METHOD_HANDLERS``. Every rule below comes from recordings; ``data/model.json`` ``notes`` lists
the few that come from documentation.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from importlib.resources import files
from typing import TYPE_CHECKING, Final
from urllib.parse import unquote_to_bytes

from fauxcall.runtime import RpcError

from fauxcall_everything import tasks
from fauxcall_everything.jsnum import (
    InvalidUrlError,
    clean_url,
    is_positive_integer,
    js_number,
    js_string,
    js_typeof,
    local_time,
    normalize_url,
)
from fauxcall_everything.zodlike import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    Field,
    Issue,
    issues_of,
    object_issue,
    prompt_arguments_error,
    protocol_error,
    record_issue,
    require_string,
    string_values_issues,
    tool_arguments_error,
    tool_error,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Response, WebSocket
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

    ToolHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]
    Completer = Callable[[str, JsonObject], list[str]]

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

METHOD_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP method -> custom handler of its params (``None``: the recordings answer)."""

ASSETS: Final = files("fauxcall_everything") / "assets"
DEFAULT_ZONE: Final = "America/New_York"
"""The recorded server's local zone (its times were UTC-4 in October): ``FAUXCALL_LOCAL_TZ``."""

STATIC_PREFIX: Final = "demo://resource/static/document/"
SESSION_PREFIX: Final = "demo://resource/session/"
DYNAMIC: Final = re.compile(r"demo://resource/dynamic/(text|blob)/([^/,]+)")
TEXT_TEMPLATE: Final = "demo://resource/dynamic/text/{resourceId}"
BLOB_TEMPLATE: Final = "demo://resource/dynamic/blob/{resourceId}"
STATIC_DOCS: Final = (
    "architecture.md",
    "extension.md",
    "features.md",
    "how-it-works.md",
    "instructions.md",
    "startup.md",
    "structure.md",
)

INVALID_TASK: Final = "MCP error -32602: Invalid task creation result: " + json.dumps(
    [
        {
            "expected": "object",
            "code": "invalid_type",
            "path": ["task"],
            "message": "Invalid input: expected object, received undefined",
        }
    ],
    indent=2,
)
"""A ``task`` on a tool that doesn't take one, or a task tool refusing its arguments."""

REQUEST_TIMED_OUT: Final = "MCP error -32001: Request timed out"
CLIENT_REQUEST_TIMEOUT_S: Final = 60.0
"""The MCP SDK's default request timeout, for requests the server sends the client."""


def _asset_text(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _asset_bytes(name: str) -> bytes:
    return (ASSETS / name).read_bytes()


def _zone(ctx: RequestContext) -> str:
    return str(getattr(ctx.runtime.settings, "local_tz", None) or DEFAULT_ZONE)


def _session(ctx: RequestContext) -> JsonObject:
    """State of the calling MCP session (the real server keeps it per ``Mcp-Session-Id``)."""
    sessions: dict[str, JsonObject] = ctx.state.setdefault("mcp_sessions", {})
    return sessions.setdefault(ctx.mcp_session or "", {})


def _call_params(ctx: RequestContext) -> JsonObject:
    message = ctx.json_body if isinstance(ctx.json_body, dict) else {}
    params = message.get("params")
    return params if isinstance(params, dict) else {}


def _text(text: str) -> JsonObject:
    return {"type": "text", "text": text}


def _tool(name: str, *, task: bool = False) -> Callable[[ToolHandler], ToolHandler]:
    """Register a tool handler; tools without task support refuse a ``task`` like the SDK."""

    def register(handler: ToolHandler) -> ToolHandler:
        async def guarded(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
            params = _call_params(ctx)
            if "task" in params and not isinstance(params["task"], dict):
                raise protocol_error([object_issue("task", params["task"])])
            if not task and "task" in params:
                raise RpcError(INVALID_PARAMS, INVALID_TASK)
            return await handler(ctx, arguments)

        TOOL_HANDLERS[name] = guarded
        return handler

    return register


# -- dynamic resources ---------------------------------------------------------------------------


def dynamic_resource(ctx: RequestContext, kind: str, number: float, uri: str) -> JsonObject:
    """``demo://resource/dynamic/{text|blob}/{id}``: made on the fly with the local time."""
    label = js_string(number)
    created = local_time(ctx.now_ns, _zone(ctx))
    if kind == "text":
        return {
            "uri": uri,
            "mimeType": "text/plain",
            "text": f"Resource {label}: This is a plaintext resource created at {created}",
        }
    text = f"Resource {label}: This is a base64 blob created at {created}"
    return {
        "uri": uri,
        "mimeType": "text/plain",
        "blob": base64.b64encode(text.encode()).decode(),
    }


# -- tools ---------------------------------------------------------------------------------------

ECHO: Final = (Field("message", "string", required=True),)
ANNOTATED: Final = (
    Field("messageType", "enum", required=True, values=("error", "success", "debug")),
    Field("includeImage", "boolean"),
)
LINKS: Final = (Field("count", "number", minimum=1, maximum=10),)
REFERENCE: Final = (
    Field("resourceType", "enum", values=("Text", "Blob")),
    Field("resourceId", "number"),
)
STRUCTURED: Final = (
    Field("location", "enum", required=True, values=("New York", "Chicago", "Los Angeles")),
)
SUM: Final = (Field("a", "number", required=True), Field("b", "number", required=True))
GZIP: Final = (
    Field("name", "string"),
    Field("data", "url"),
    Field("outputType", "enum", values=("resourceLink", "resource")),
)
LONG: Final = (Field("duration", "number"), Field("steps", "number"))
RESEARCH: Final = (Field("topic", "string", required=True), Field("ambiguous", "boolean"))
SAMPLING: Final = (Field("prompt", "string", required=True), Field("maxTokens", "number"))

ANNOTATED_MESSAGES: Final[dict[str, JsonObject]] = {
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
WEATHER: Final[dict[str, JsonObject]] = {
    "New York": {"temperature": 33, "conditions": "Cloudy", "humidity": 82},
    "Chicago": {"temperature": 36, "conditions": "Light rain / drizzle", "humidity": 82},
    "Los Angeles": {"temperature": 73, "conditions": "Sunny / Clear", "humidity": 48},
}


@_tool("echo")
async def echo(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``Echo: <message>``."""
    del ctx
    if issues := issues_of(ECHO, arguments):
        return tool_arguments_error("echo", issues)
    return {"content": [_text(f"Echo: {arguments['message']}")]}


@_tool("get-annotated-message")
async def get_annotated_message(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """A text block annotated per ``messageType``, optionally the tiny image (user, 0.5)."""
    del ctx
    if issues := issues_of(ANNOTATED, arguments):
        return tool_arguments_error("get-annotated-message", issues)
    content: list[JsonValue] = [ANNOTATED_MESSAGES[str(arguments["messageType"])]]
    if arguments.get("includeImage") is True:
        content.append(
            {
                "type": "image",
                "data": _asset_text("tiny_image.b64"),
                "mimeType": "image/png",
                "annotations": {"audience": ["user"], "priority": 0.5},
            }
        )
    return {"content": content}


@_tool("get-env")
async def get_env(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """The server process's environment, as recorded (it was started with a near-empty one)."""
    del ctx, arguments
    return {"content": [_text(_asset_text("env.txt"))]}


@_tool("get-resource-links")
async def get_resource_links(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """An intro, then links 1..count alternating Blob (odd) and Text (even) resources."""
    del ctx
    if issues := issues_of(LINKS, arguments):
        return tool_arguments_error("get-resource-links", issues)
    count = arguments.get("count", 3)
    assert isinstance(count, int | float)  # noqa: S101 - validated above
    content: list[JsonValue] = [
        _text(f"Here are {js_string(count)} resource links to resources available in this server:")
    ]
    index = 1
    while index <= count:
        kind = "Blob" if index % 2 else "Text"
        content.append(
            {
                "name": f"{kind} Resource {index}",
                "uri": f"demo://resource/dynamic/{kind.lower()}/{index}",
                "description": f"Resource {index}: plaintext resource",
                "mimeType": "text/plain",
                "type": "resource_link",
            }
        )
        index += 1
    return {"content": content}


@_tool("get-resource-reference")
async def get_resource_reference(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """The dynamic resource itself, between two explanatory texts."""
    if issues := issues_of(REFERENCE, arguments):
        return tool_arguments_error("get-resource-reference", issues)
    kind = str(arguments.get("resourceType", "Text")).lower()
    number = arguments.get("resourceId", 1)
    assert isinstance(number, int | float)  # noqa: S101 - validated above
    if not is_positive_integer(float(number)):
        return tool_error(
            f"Invalid resourceId: {js_string(number)}. Must be a finite positive integer."
        )
    uri = f"demo://resource/dynamic/{kind}/{js_string(number)}"
    return {
        "content": [
            _text(f"Returning resource reference for Resource {js_string(number)}:"),
            {"type": "resource", "resource": dynamic_resource(ctx, kind, number, uri)},
            _text(f"You can access this resource using the URI: {uri}"),
        ]
    }


@_tool("get-structured-content")
async def get_structured_content(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Fixed weather per city, as JSON text and as ``structuredContent``."""
    del ctx
    if issues := issues_of(STRUCTURED, arguments):
        return tool_arguments_error("get-structured-content", issues)
    weather = WEATHER[str(arguments["location"])]
    text = json.dumps(weather, separators=(",", ":"))
    return {"content": [_text(text)], "structuredContent": dict(weather)}


@_tool("get-sum")
async def get_sum(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``The sum of a and b is a+b.`` in JavaScript number formatting."""
    del ctx
    if issues := issues_of(SUM, arguments):
        return tool_arguments_error("get-sum", issues)
    a, b = arguments["a"], arguments["b"]
    assert isinstance(a, int | float)  # noqa: S101 - validated above
    assert isinstance(b, int | float)  # noqa: S101
    total = float(a) + float(b)
    return {
        "content": [_text(f"The sum of {js_string(a)} and {js_string(b)} is {js_string(total)}.")]
    }


@_tool("get-tiny-image")
async def get_tiny_image(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """The MCP logo PNG between two texts."""
    del ctx, arguments
    return {
        "content": [
            _text("Here's the image you requested:"),
            {"type": "image", "data": _asset_text("tiny_image.b64"), "mimeType": "image/png"},
            _text("The image above is the MCP logo."),
        ]
    }


@_tool("trigger-long-running-operation")
async def long_running_operation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Sleep ``duration/steps`` per step; progress notifications only with a progressToken."""
    if issues := issues_of(LONG, arguments):
        return tool_arguments_error("trigger-long-running-operation", issues)
    duration = arguments.get("duration", 10)
    steps = arguments.get("steps", 5)
    assert isinstance(duration, int | float)  # noqa: S101 - validated above
    assert isinstance(steps, int | float)  # noqa: S101
    meta = _call_params(ctx).get("_meta")
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    step = 1
    while step < steps + 1:
        await ctx.pause(max(duration / steps, 0))
        if token is not None:
            ctx.notify(
                "notifications/progress", {"progress": step, "total": steps, "progressToken": token}
            )
        step += 1
    return {
        "content": [
            _text(
                f"Long running operation completed. Duration: {js_string(duration)} seconds, "
                f"Steps: {js_string(steps)}."
            )
        ]
    }


# -- gzip-file-as-resource -----------------------------------------------------------------------

DEFAULT_GZIP_NAME: Final = "README.md.gz"
DEFAULT_GZIP_DATA: Final = (
    "https://raw.githubusercontent.com/modelcontextprotocol/servers/refs/heads/main/README.md"
)
FETCHED: Final[dict[str, tuple[str, float]]] = {
    DEFAULT_GZIP_DATA: ("fetched/readme.bin", 0.248),
    "https://example.com/index.html": ("fetched/example.bin", 0.150),
    "http://example.com/": ("fetched/example.bin", 0.110),
    "https://raw.githubusercontent.com/modelcontextprotocol/servers/refs/heads/main/"
    "DOES-NOT-EXIST.md": ("fetched/github-404.bin", 0.081),
}
"""URLs fetched while recording: their bodies and how long the fetch took. The fake is offline."""
UNREACHABLE_S: Final = 0.052
"""How long the real server took to give up on a host it couldn't reach."""


def _forgiving_base64(text: str) -> bytes | None:
    """WHATWG forgiving-base64 decode (what ``fetch`` uses for ``;base64`` data URLs)."""
    compact = re.sub(r"[\t\n\f\r ]", "", text)
    if len(compact) % 4 == 0:
        compact = (
            compact.removesuffix("==") if compact.endswith("==") else compact.removesuffix("=")
        )
    if len(compact) % 4 == 1 or not re.fullmatch(r"[A-Za-z0-9+/]*", compact):
        return None
    try:
        return base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
    except binascii.Error:  # pragma: no cover - the pattern above already excludes it
        return None


def decode_data_url(url: str) -> bytes | None:
    """The body ``fetch`` reads from a ``data:`` URL, or ``None`` when it fails.

    The URL is parsed first: its fragment is not part of the body.
    """
    header, comma, payload = clean_url(url).partition("#")[0].removeprefix("data:").partition(",")
    if not comma:
        return None
    body = unquote_to_bytes(payload)
    if re.search(r";[\t\n\f\r ]*base64[\t\n\f\r ]*$", header, re.IGNORECASE):
        return _forgiving_base64(body.decode("latin-1"))
    return body


def gzip_bytes(data: bytes) -> bytes:
    """``zlib.gzipSync`` from Node: default level, mtime 0, OS byte 0x13 (byte-identical)."""
    compressor = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, 31)
    packed = compressor.compress(data) + compressor.flush()
    return packed[:9] + b"\x13" + packed[10:]


async def _fetch(ctx: RequestContext, url: str) -> bytes | JsonObject:
    """What the server fetched, or the error result it answered."""
    url = clean_url(url)
    scheme = url.partition(":")[0].lower()
    if scheme == "data":
        body = decode_data_url(url)
        return body if body is not None else tool_error("fetch failed")
    if scheme not in {"http", "https"}:
        return tool_error(
            f"Error processing file {url}: Unsupported URL protocol for {url}. "
            "Only http, https, and data URLs are supported."
        )
    known = FETCHED.get(normalize_url(url))
    if known is None:
        await ctx.pause(UNREACHABLE_S)
        return tool_error("fetch failed")
    asset, seconds = known
    await ctx.pause(seconds)
    return _asset_bytes(asset)


@_tool("gzip-file-as-resource")
async def gzip_file_as_resource(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Fetch, gzip, register as a session resource, answer a link or the resource itself."""
    if issues := issues_of(GZIP, arguments):
        return tool_arguments_error("gzip-file-as-resource", issues)
    name = str(arguments.get("name", DEFAULT_GZIP_NAME))
    url = str(arguments.get("data", DEFAULT_GZIP_DATA))
    fetched = await _fetch(ctx, url)
    if isinstance(fetched, dict):
        return fetched
    blob = base64.b64encode(gzip_bytes(fetched)).decode()
    uri = f"{SESSION_PREFIX}{name}"
    resources = _session_resources(ctx)
    resources.pop(uri, None)  # a name registered again moves to the end
    resources[uri] = blob  # under the raw URI: reads look up the normalized one (recorded)
    if arguments.get("outputType") == "resource":
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


def _session_resources(ctx: RequestContext) -> dict[str, str]:
    """This MCP session's gzip resources: raw URI -> base64 blob."""
    return _session(ctx).setdefault("resources", {})  # type: ignore[return-value]


# -- toggles -------------------------------------------------------------------------------------


def _session_label(ctx: RequestContext) -> str:
    return ctx.mcp_session or "undefined"


@_tool("toggle-simulated-logging")
async def toggle_simulated_logging(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Start or stop the session's simulated logging (alternates, per session)."""
    del arguments
    state = _session(ctx)
    state["logging"] = not state.get("logging", False)
    sid = _session_label(ctx)
    if state["logging"]:
        text = (
            f"Started simulated, random-leveled logging for session {sid} at a 5 second pace. "
            "Client's selected logging level will be respected. If an interval elapses and the "
            "message to be sent is below the selected level, it will not be sent. Thus at higher "
            "chosen logging levels, messages should arrive further apart. "
        )
    else:
        text = f"Stopped simulated logging for session {sid}"
    return {"content": [_text(text)]}


@_tool("toggle-subscriber-updates")
async def toggle_subscriber_updates(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Start or stop the session's simulated resource-update notifications."""
    del arguments
    state = _session(ctx)
    state["updates"] = not state.get("updates", False)
    sid = _session_label(ctx)
    if state["updates"]:
        text = (
            f"Started simulated resource updated notifications for session {sid} at a 5 second "
            "pace. Client will receive updates for any resources the it is subscribed to."
        )
    else:
        text = f"Stopped simulated resource updates for session {sid}"
    return {"content": [_text(text)]}


# -- tasks ---------------------------------------------------------------------------------------


@_tool("simulate-research-query", task=True)
async def simulate_research_query(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Requires task augmentation: creates a task that completes in about 4 s."""
    if "task" not in _call_params(ctx):
        return tool_error(
            "MCP error -32601: Tool simulate-research-query requires task augmentation "
            "(taskSupport: 'required')"
        )
    if issues_of(RESEARCH, arguments):
        raise RpcError(INVALID_PARAMS, INVALID_TASK)
    return tasks.create(ctx, str(arguments["topic"]))


# -- tools that call back into the client --------------------------------------------------------


async def _client_never_answers(ctx: RequestContext) -> JsonValue:
    """The server's request to the client (sampling, elicitation, roots) is never answered."""
    await ctx.pause(CLIENT_REQUEST_TIMEOUT_S)
    return tool_error(REQUEST_TIMED_OUT)


@_tool("trigger-sampling-request")
async def trigger_sampling_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Asks the client's LLM via ``sampling/createMessage``: the fake can't send it."""
    if issues := issues_of(SAMPLING, arguments):
        return tool_arguments_error("trigger-sampling-request", issues)
    return await _client_never_answers(ctx)


@_tool("trigger-elicitation-request")
async def trigger_elicitation_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Asks the user via ``elicitation/create``: the fake can't send it."""
    del arguments
    return await _client_never_answers(ctx)


NO_ROOTS: Final = (
    "The client supports roots but no roots are currently configured.\n\n"
    "This could mean:\n"
    "1. The client hasn't provided any roots yet\n"
    "2. The client provided an empty roots list\n"
    "3. The roots configuration is still being loaded"
)


@_tool("get-roots-list")
async def get_roots_list(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """The roots the client last sent: the fake never asks, so there are none (observed)."""
    del ctx, arguments
    return {"content": [_text(NO_ROOTS)]}


# -- other methods -------------------------------------------------------------------------------


async def read_resource(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Static documents, dynamic text/blob resources and this session's gzip resources."""
    uri = require_string(params, "uri")
    try:
        href = normalize_url(uri)
    except InvalidUrlError:
        raise RpcError(INTERNAL_ERROR, "Invalid URL") from None
    if href.startswith(STATIC_PREFIX) and href.removeprefix(STATIC_PREFIX) in STATIC_DOCS:
        text = _asset_text(f"docs/{href.removeprefix(STATIC_PREFIX)}")
        return {"contents": [{"uri": href, "mimeType": "text/markdown", "text": text}]}
    dynamic = DYNAMIC.fullmatch(href)
    if dynamic is not None:
        number = js_number(dynamic.group(2))
        if not is_positive_integer(number):
            raise RpcError(INTERNAL_ERROR, f"Unknown resource: {href}")
        return {"contents": [dynamic_resource(ctx, dynamic.group(1), number, href)]}
    resources = _session_resources(ctx)
    if href in resources:
        blob = resources[href]
        return {"contents": [{"uri": href, "mimeType": "application/gzip", "blob": blob}]}
    raise RpcError(INVALID_PARAMS, f"MCP error -32602: Resource {href} not found")


async def subscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/subscribe``: any URI is accepted, even unknown ones (recorded)."""
    uri = require_string(params, "uri")
    subscriptions: list[str] = _session(ctx).setdefault("subscriptions", [])  # type: ignore[assignment]
    if uri not in subscriptions:
        subscriptions.append(uri)
    return {}


async def unsubscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/unsubscribe``: accepted for URIs never subscribed too (recorded)."""
    uri = require_string(params, "uri")
    subscriptions = _session(ctx).get("subscriptions")
    if isinstance(subscriptions, list) and uri in subscriptions:
        subscriptions.remove(uri)
    return {}


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


async def set_level(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``logging/setLevel``: one of the eight syslog levels."""
    level = params.get("level")
    if level not in LOG_LEVELS:
        options = "|".join(json.dumps(option) for option in LOG_LEVELS)
        raise protocol_error(
            [
                Issue(
                    ["level"],
                    f"Invalid option: expected one of {options}",
                    {"code": "invalid_value", "values": list(LOG_LEVELS)},
                )
            ]
        )
    _session(ctx)["level"] = level
    return {}


# -- prompts -------------------------------------------------------------------------------------

ARGS_PROMPT: Final = (Field("city", "string", required=True), Field("state", "string"))
COMPLETABLE_PROMPT: Final = (
    Field("department", "string", required=True),
    Field("name", "string", required=True),
)
RESOURCE_PROMPT: Final = (
    Field("resourceType", "string", required=True),
    Field("resourceId", "string", required=True),
)


def _user(content: JsonObject) -> JsonObject:
    return {"role": "user", "content": content}


def _prompt_messages(ctx: RequestContext, name: str, arguments: JsonObject) -> list[JsonValue]:
    if name == "simple-prompt":
        return [_user(_text("This is a simple prompt without arguments."))]
    if name == "args-prompt":
        if issues := issues_of(ARGS_PROMPT, arguments):
            raise prompt_arguments_error(name, issues)
        city, state = arguments["city"], arguments.get("state")
        where = f"{city}, {state}" if state else f"{city}"
        return [_user(_text(f"What's weather in {where}?"))]
    if name == "completable-prompt":
        if issues := issues_of(COMPLETABLE_PROMPT, arguments):
            raise prompt_arguments_error(name, issues)
        return [
            _user(
                _text(
                    f"Please promote {arguments['name']} to the head of the "
                    f"{arguments['department']} team."
                )
            )
        ]
    if issues := issues_of(RESOURCE_PROMPT, arguments):
        raise prompt_arguments_error(name, issues)
    kind, raw_id = str(arguments["resourceType"]), str(arguments["resourceId"])
    if kind not in {"Text", "Blob"}:
        raise RpcError(INTERNAL_ERROR, f"Invalid resourceType: {kind}. Must be Text or Blob.")
    number = js_number(raw_id)
    if not is_positive_integer(number):
        raise RpcError(
            INTERNAL_ERROR, f"Invalid resourceId: {raw_id}. Must be a finite positive integer."
        )
    uri = f"demo://resource/dynamic/{kind.lower()}/{js_string(number)}"
    return [
        _user(
            _text(
                f"This prompt includes the {kind} resource with id: {js_string(number)}. "
                "Please analyze the following resource:"
            )
        ),
        _user({"type": "resource", "resource": dynamic_resource(ctx, kind.lower(), number, uri)}),
    ]


PROMPT_NAMES: Final = ("simple-prompt", "args-prompt", "completable-prompt", "resource-prompt")


async def get_prompt(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``prompts/get`` of the four prompts."""
    name = require_string(params, "name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise protocol_error([record_issue("arguments", arguments)])
    if issues := string_values_issues(arguments):
        raise protocol_error(issues, ("params", "arguments"))
    if name not in PROMPT_NAMES:
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Prompt {name} not found")
    return {"messages": _prompt_messages(ctx, name, arguments)}


# -- completions ---------------------------------------------------------------------------------

DEPARTMENTS: Final = ("Engineering", "Sales", "Marketing", "Support")
TEAM: Final[dict[str, tuple[str, ...]]] = {
    "Engineering": ("Alice", "Bob", "Charlie"),
    "Sales": ("David", "Eve", "Frank"),
    "Marketing": ("Grace", "Henry", "Iris"),
    "Support": ("John", "Kim", "Lee"),
}


def _completed(values: list[str]) -> JsonObject:
    return {"completion": {"values": list(values), "total": len(values), "hasMore": False}}


NO_COMPLETER: Final[JsonObject] = {"completion": {"values": [], "hasMore": False}}


def _resource_id_values(value: str, known: JsonObject) -> list[str]:
    del known
    return [value] if value and is_positive_integer(js_number(value)) else []


def _departments(value: str, known: JsonObject) -> list[str]:
    del known
    return [department for department in DEPARTMENTS if department.startswith(value)]


def _team_members(value: str, known: JsonObject) -> list[str]:
    team = TEAM.get(str(known.get("department")), ())
    return [member for member in team if member.startswith(value)]


def _resource_types(value: str, known: JsonObject) -> list[str]:
    del known
    return [kind for kind in ("Text", "Blob") if kind.startswith(value)]


PROMPT_COMPLETERS: Final[dict[tuple[str, str], Completer]] = {
    ("completable-prompt", "department"): _departments,
    ("completable-prompt", "name"): _team_members,
    ("resource-prompt", "resourceType"): _resource_types,
    ("resource-prompt", "resourceId"): _resource_id_values,
}
"""Prompt arguments with a completer; the others answer no values (and no ``total``)."""


def _string_issue(path: list[str | int], value: JsonValue, *, present: bool) -> Issue:
    return Issue(
        path,
        f"Invalid input: expected string, received {js_typeof(value, present=present)}",
        {"expected": "string", "code": "invalid_type"},
    )


def _ref_issues(ref: JsonValue) -> list[Issue]:
    """``params.ref``: a prompt or resource reference; anything else fails both branches."""
    reference = ref if isinstance(ref, dict) else {}
    branches = {"ref/prompt": "name", "ref/resource": "uri"}
    kind = reference.get("type")
    if kind in branches and isinstance(reference.get(branches[str(kind)]), str):
        return []
    errors: list[JsonValue] = []
    for literal, target in branches.items():
        branch: list[JsonValue] = []
        if kind != literal:
            branch.append(
                {
                    "code": "invalid_value",
                    "values": [literal],
                    "path": ["type"],
                    "message": f'Invalid input: expected "{literal}"',
                }
            )
        if not isinstance(reference.get(target), str):
            missing = _string_issue([target], reference.get(target), present=target in reference)
            branch.append(missing.as_json())
        errors.append(branch)
    return [Issue(["ref"], "Invalid input", {"code": "invalid_union", "errors": errors})]


def _complete_resource(ctx: RequestContext, uri: str, name: str, value: str) -> JsonObject:
    if uri in {TEXT_TEMPLATE, BLOB_TEMPLATE}:
        return _completed(_resource_id_values(value, {})) if name == "resourceId" else NO_COMPLETER
    static = uri.startswith(STATIC_PREFIX) and uri.removeprefix(STATIC_PREFIX) in STATIC_DOCS
    if static or uri in _session_resources(ctx):
        return NO_COMPLETER
    raise RpcError(INVALID_PARAMS, f"MCP error -32602: Resource template {uri} not found")


async def complete(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``completion/complete`` for the completable prompts and the dynamic resource templates."""
    ref, argument = params.get("ref"), params.get("argument")
    issues = _ref_issues(ref)
    if isinstance(argument, dict):
        issues += [
            _string_issue(["argument", key], argument.get(key), present=key in argument)
            for key in ("name", "value")
            if not isinstance(argument.get(key), str)
        ]
    else:
        issues.append(
            Issue(
                ["argument"],
                "Invalid input: expected object, received "
                + js_typeof(argument, present="argument" in params),
                {"expected": "object", "code": "invalid_type"},
            )
        )
    if issues or not isinstance(ref, dict) or not isinstance(argument, dict):
        raise protocol_error(issues)
    name, value = str(argument.get("name", "")), str(argument.get("value", ""))
    if ref["type"] == "ref/resource":
        return _complete_resource(ctx, str(ref["uri"]), name, value)
    prompt = str(ref["name"])
    if prompt not in PROMPT_NAMES:
        raise RpcError(INVALID_PARAMS, f"MCP error -32602: Prompt {prompt} not found")
    completer = PROMPT_COMPLETERS.get((prompt, name))
    if completer is None:
        return NO_COMPLETER
    context = params.get("context")
    given = context.get("arguments") if isinstance(context, dict) else None
    return _completed(completer(value, given if isinstance(given, dict) else {}))


# -- tasks/* -------------------------------------------------------------------------------------


async def task_get(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/get``."""
    return tasks.get(ctx, require_string(params, "taskId"))


async def task_result(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/result`` (waits for completion)."""
    return await tasks.result(ctx, require_string(params, "taskId"))


async def task_cancel(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/cancel``."""
    return tasks.cancel(ctx, require_string(params, "taskId"))


async def task_list(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tasks/list``."""
    if "cursor" in params:
        require_string(params, "cursor")
    return tasks.listing(ctx, params)


METHOD_HANDLERS.update(
    {
        "resources/read": read_resource,
        "resources/subscribe": subscribe,
        "resources/unsubscribe": unsubscribe,
        "logging/setLevel": set_level,
        "prompts/get": get_prompt,
        "completion/complete": complete,
        "tasks/get": task_get,
        "tasks/result": task_result,
        "tasks/cancel": task_cancel,
        "tasks/list": task_list,
    }
)
