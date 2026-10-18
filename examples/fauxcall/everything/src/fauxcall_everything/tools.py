"""The real server's tools, one handler each (``TOOL_HANDLERS``).

Each handler validates its arguments as the tool's zod schema does (defaults filled in, the SDK's
refusal text otherwise), then answers exactly as the TypeScript implementation does: same texts,
same member order, numbers printed the JavaScript way. Errors a tool throws become ``isError``
results with the error's message, like ``createToolError``.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import random
import re
import time
import uuid
import zlib
from typing import TYPE_CHECKING, Final
from urllib.parse import unquote_to_bytes

from fauxcall.runtime import ClientError, RpcError

from fauxcall_everything import js, zod
from fauxcall_everything.assets import ASYNC_ELICITATION_FORM, ELICITATION_FORM, MCP_TINY_IMAGE
from fauxcall_everything.resources import (
    blob_resource,
    register_session_resource,
    text_resource,
)
from fauxcall_everything.state import log_message, server_of, session_of, stop, transport_of
from fauxcall_everything.validation import (
    InvalidAnswerError,
    create_message_result,
    elicit_result,
    list_roots_result,
    poll_result,
    task_or,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.types import JsonObject, JsonValue

    from fauxcall_everything.state import McpSession, Server

    ToolHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]

SIMULATION_SECONDS: Final = 5.0
"""The pace of simulated log messages and resource updates."""
REQUEST_TIMEOUT_S: Final = 60.0
ELICITATION_TIMEOUT_S: Final = 600.0
GZIP_MAX_FETCH_SIZE: Final = 10 * 1024 * 1024
README_URL: Final = (
    "https://raw.githubusercontent.com/modelcontextprotocol/servers/refs/heads/main/README.md"
)
README_RECORDING: Final = "MCP resources/read:demo://resource/session/README.md.gz"
README_FETCH_SECONDS: Final = 0.128
"""How much longer the default README call took than a data: URL (132 ms vs about 4 ms)."""
REQUEST_TIMED_OUT: Final = -32001
RESOURCE_TYPES: Final = ("Text", "Blob")
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
WEATHER: Final[dict[str, JsonObject]] = {
    "New York": {"temperature": 33, "conditions": "Cloudy", "humidity": 82},
    "Chicago": {"temperature": 36, "conditions": "Light rain / drizzle", "humidity": 82},
    "Los Angeles": {"temperature": 73, "conditions": "Sunny / Clear", "humidity": 48},
}
SYSTEM_PROMPT: Final = "You are a helpful test server."
ASYNC_POLL_SECONDS: Final = 1.0
ASYNC_SAMPLING_POLLS: Final = 60
ASYNC_ELICITATION_POLLS: Final = 600


def text(value: str) -> JsonObject:
    """A text content block."""
    return {"type": "text", "text": value}


def tool_error(message: str) -> JsonObject:
    """``createToolError``: a thrown error as the tool's result."""
    return {"content": [text(message)], "isError": True}


def client_error_text(error: ClientError) -> str:
    """An ``McpError``'s message: ``MCP error -1: User rejected sampling request``."""
    return f"MCP error {error.code}: {error.message}"


def schema(tool: str, /, **fields: zod.Field) -> zod.ToolSchema:
    """A tool's input schema."""
    return zod.ToolSchema(tool, dict(fields))


ECHO: Final = schema("echo", message=zod.Field(zod.string()))
ANNOTATED: Final = schema(
    "get-annotated-message",
    messageType=zod.Field(zod.enum("error", "success", "debug")),
    includeImage=zod.Field(zod.boolean(), default=False),
)
LINKS: Final = schema(
    "get-resource-links", count=zod.Field(zod.number(minimum=1, maximum=10), default=3)
)
REFERENCE: Final = schema(
    "get-resource-reference",
    resourceType=zod.Field(zod.enum(*RESOURCE_TYPES), default="Text"),
    resourceId=zod.Field(zod.number(), default=1),
)
STRUCTURED: Final = schema(
    "get-structured-content",
    location=zod.Field(zod.enum("New York", "Chicago", "Los Angeles")),
)
SUM: Final = schema("get-sum", a=zod.Field(zod.number()), b=zod.Field(zod.number()))
GZIP: Final = schema(
    "gzip-file-as-resource",
    name=zod.Field(zod.string(), default="README.md.gz"),
    data=zod.Field(zod.string(url=True), default=README_URL),
    outputType=zod.Field(zod.enum("resourceLink", "resource"), default="resourceLink"),
)
LONG_RUNNING: Final = schema(
    "trigger-long-running-operation",
    duration=zod.Field(zod.number(), default=10),
    steps=zod.Field(zod.number(), default=5),
)
SAMPLING: Final = schema(
    "trigger-sampling-request",
    prompt=zod.Field(zod.string()),
    maxTokens=zod.Field(zod.number(), default=100),
)
SAMPLING_ASYNC: Final = schema(
    "trigger-sampling-request-async",
    prompt=zod.Field(zod.string()),
    maxTokens=zod.Field(zod.number(), default=100),
)
URL_ELICITATION: Final = schema(
    "trigger-url-elicitation",
    url=zod.Field(zod.string(url=True)),
    message=zod.Field(zod.string(), default="Please open the link to complete this action."),
    elicitationId=zod.Field(zod.string(), optional=True),
    errorPath=zod.Field(zod.boolean(), default=False),
)


# -- simple tools ----------------------------------------------------------------------------------


async def echo(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``echo``: ``Echo: <message>``."""
    args, refusal = ECHO.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    return {"content": [text(f"Echo: {args['message']}")]}


async def get_annotated_message(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-annotated-message``: one annotated text, and the logo when asked."""
    args, refusal = ANNOTATED.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    messages: dict[str, tuple[str, list[JsonValue], JsonValue]] = {
        "error": ("Error: Operation failed", ["user", "assistant"], 1),
        "success": ("Operation completed successfully", ["user"], js.json_number(0.7)),
        "debug": ("Debug: Cache hit ratio 0.95, latency 150ms", ["assistant"], js.json_number(0.3)),
    }
    message, audience, priority = messages[str(args["messageType"])]
    content: list[JsonValue] = [
        {
            "type": "text",
            "text": message,
            "annotations": {"audience": audience, "priority": priority},
        }
    ]
    if args["includeImage"]:
        content.append(
            {
                "type": "image",
                "data": MCP_TINY_IMAGE,
                "mimeType": "image/png",
                "annotations": {"audience": ["user"], "priority": js.json_number(0.5)},
            }
        )
    return {"content": content}


async def get_tiny_image(_ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """``get-tiny-image``: the MCP logo between two texts."""
    return {
        "content": [
            text("Here's the image you requested:"),
            {"type": "image", "data": MCP_TINY_IMAGE, "mimeType": "image/png"},
            text("The image above is the MCP logo."),
        ]
    }


async def get_resource_links(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-resource-links``: links to blob (odd ids) and text (even ids) dynamic resources."""
    args, refusal = LINKS.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    count = js.as_number(args["count"])
    content: list[JsonValue] = [
        text(
            f"Here are {js.js_number(count)} resource links to resources available in this server:"
        )
    ]
    resource_id = 1
    while resource_id <= count:
        even = resource_id % 2 == 0
        resource = (text_resource if even else blob_resource)(ctx, float(resource_id))
        content.append(
            {
                "name": f"{'Text' if even else 'Blob'} Resource {resource_id}",
                "uri": resource["uri"],
                "description": f"Resource {resource_id}: plaintext resource",
                "mimeType": resource["mimeType"],
                "type": "resource_link",
            }
        )
        resource_id += 1
    return {"content": content}


async def get_resource_reference(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-resource-reference``: an embedded dynamic resource and its URI."""
    args, refusal = REFERENCE.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    resource_id = js.as_number(args["resourceId"])
    if not js.is_positive_integer(resource_id):
        shown = js.js_string(args["resourceId"])
        return tool_error(f"Invalid resourceId: {shown}. Must be a finite positive integer.")
    make = text_resource if args["resourceType"] == "Text" else blob_resource
    resource = make(ctx, resource_id)
    return {
        "content": [
            text(f"Returning resource reference for Resource {js.js_number(resource_id)}:"),
            {"type": "resource", "resource": resource},
            text(f"You can access this resource using the URI: {resource['uri']}"),
        ]
    }


async def get_structured_content(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-structured-content``: the city's (fixed) weather, as text and structured content."""
    args, refusal = STRUCTURED.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    weather = WEATHER[str(args["location"])]
    return {"content": [text(js.js_stringify(weather))], "structuredContent": dict(weather)}


async def get_sum(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``get-sum``: ``The sum of a and b is a+b.``, in double arithmetic."""
    args, refusal = SUM.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    a, b = js.as_number(args["a"]), js.as_number(args["b"])
    total = a + b
    return {
        "content": [
            text(f"The sum of {js.js_number(a)} and {js.js_number(b)} is {js.js_number(total)}.")
        ]
    }


# -- gzip ------------------------------------------------------------------------------------------


class FetchError(Exception):
    """``fetch`` (or reading its body) failed; the message is Node's."""


def node_gzip(data: bytes) -> bytes:
    """``gzipSync(data)``: Node's header (no mtime, OS 19), level 6, CRC32 and size."""
    compressor = zlib.compressobj(6, zlib.DEFLATED, -15, 8, 0)
    body = compressor.compress(data) + compressor.flush()
    header = bytes([0x1F, 0x8B, 8, 0, 0, 0, 0, 0, 0, 0x13])
    trailer = (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "little")
    return header + body + trailer + (len(data) & 0xFFFFFFFF).to_bytes(4, "little")


def _forgiving_base64(data: str) -> bytes:
    """The fetch spec's forgiving-base64 decode (``FetchError`` where it fails)."""
    cleaned = "".join(char for char in data if char not in " \t\n\f\r")
    if len(cleaned) % 4 == 0 and cleaned.endswith("=="):
        cleaned = cleaned[:-2]
    elif len(cleaned) % 4 == 0 and cleaned.endswith("="):
        cleaned = cleaned[:-1]
    if len(cleaned) % 4 == 1 or any(
        not (char.isascii() and (char.isalnum() or char in "+/")) for char in cleaned
    ):
        raise FetchError("fetch failed")
    return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4))


def fetch_data_url(href: str) -> bytes:
    """What ``fetch`` reads from a ``data:`` URL."""
    body = href[len("data:") :]
    if "#" in body:
        body = body.split("#", 1)[0]
    if "," not in body:
        raise FetchError("fetch failed")
    mime, payload = body.split(",", 1)
    raw = unquote_to_bytes(payload)
    if _BASE64_PARAMETER.search(mime.strip(" \t\n\f\r")):  # ";[ ]*base64" ends the type
        return _forgiving_base64(raw.decode("latin-1"))
    return raw


_BASE64_PARAMETER: Final = re.compile(r";[ ]*base64$", re.IGNORECASE)


def fetch_recorded(ctx: RequestContext, href: str) -> bytes:
    """An http(s) fetch: the fake never goes out, it serves what was recorded.

    The default README was recorded; anything else fails as ``fetch`` does without a network.
    """
    if href != README_URL:
        raise FetchError("fetch failed")
    for exchange in ctx.runtime.recordings.for_endpoint(README_RECORDING):
        reply = exchange.response.body.parsed() if exchange.response else None
        result = reply.get("result") if isinstance(reply, dict) else None
        contents = result.get("contents") if isinstance(result, dict) else None
        if isinstance(contents, list) and contents and isinstance(contents[0], dict):
            return gzip.decompress(base64.b64decode(str(contents[0].get("blob"))))
    raise FetchError("fetch failed")  # pragma: no cover - the recording ships with the fake


async def gzip_file_as_resource(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``gzip-file-as-resource``: compress a data/http(s) URL's content into a session resource."""
    args, refusal = GZIP.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    name, data_uri = str(args["name"]), str(args["data"])
    url = js.parse_url(data_uri)
    assert url is not None  # noqa: S101 - the schema checked it
    if url.scheme not in {"http", "https", "data"}:
        return tool_error(
            f"Error processing file {data_uri}: Unsupported URL protocol for {data_uri}. "
            "Only http, https, and data URLs are supported."
        )
    try:
        content = (
            fetch_data_url(url.href) if url.scheme == "data" else fetch_recorded(ctx, url.href)
        )
    except FetchError as error:
        return tool_error(str(error))
    if url.scheme != "data":
        await ctx.pause(README_FETCH_SECONDS)  # the recorded download (000286)
    if len(content) > GZIP_MAX_FETCH_SIZE:
        return tool_error(f"Response from {url.href} exceeds {GZIP_MAX_FETCH_SIZE} bytes")
    blob = base64.b64encode(node_gzip(content)).decode("ascii")
    uri = f"demo://resource/session/{name}"
    register_session_resource(ctx, uri, name, "application/gzip", blob)
    if args["outputType"] == "resource":
        resource: JsonObject = {"uri": uri, "mimeType": "application/gzip", "blob": blob}
        return {"content": [{"type": "resource", "resource": resource}]}
    link: JsonObject = {
        "name": name,
        "uri": uri,
        "mimeType": "application/gzip",
        "type": "resource_link",
    }
    return {"content": [link]}


# -- simulated logging and resource updates -------------------------------------------------------


def _session_id(ctx: RequestContext) -> str:
    return ctx.mcp_session or ""


def _log_random(channel: Channel, session: McpSession, rng: random.Random) -> None:
    level, message = LOG_MESSAGES[rng.randrange(len(LOG_MESSAGES))]
    suffix = f" - SessionId {session.id}" if session.id else ""
    log_message(channel, session, level, message + suffix)


async def toggle_simulated_logging(ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """``toggle-simulated-logging``: random-leveled log messages every 5 s, or stop them."""
    server, session = server_of(ctx), session_of(ctx)
    sid = _session_id(ctx)
    if session.logging is not None:
        stop(session.logging)
        session.logging = None
        return {"content": [text(f"Stopped simulated logging for session {sid}")]}
    rng = random.Random(ctx.rng.random())  # a simulation, as Math.random() is
    assert ctx.channel is not None  # noqa: S101 - every MCP call has one
    _log_random(ctx.channel, session, rng)  # once at once, then every 5 seconds

    async def every_five_seconds(channel: Channel) -> None:
        while server.alive():
            await channel.pause(SIMULATION_SECONDS)
            _log_random(channel, session, rng)

    session.logging = ctx.background(every_five_seconds)
    return {
        "content": [
            text(
                f"Started simulated, random-leveled logging for session {sid} at a 5 second pace. "
                "Client's selected logging level will be respected. If an interval elapses and "
                "the message to be sent is below the selected level, it will not be sent. Thus "
                "at higher chosen logging levels, messages should arrive further apart. "
            )
        ]
    }


def _send_updates(channel: Channel, server: Server, session: McpSession) -> None:
    for uri, subscribers in server.subscriptions.items():
        if session.id in subscribers:
            channel.push("notifications/resources/updated", {"uri": uri})


async def toggle_subscriber_updates(ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """``toggle-subscriber-updates``: resource updates every 5 s for subscribed URIs, or stop."""
    server, session = server_of(ctx), session_of(ctx)
    sid = _session_id(ctx)
    if session.updates is not None:
        stop(session.updates)
        session.updates = None
        return {"content": [text(f"Stopped simulated resource updates for session {sid}")]}
    assert ctx.channel is not None  # noqa: S101 - every MCP call has one
    _send_updates(ctx.channel, server, session)

    async def every_five_seconds(channel: Channel) -> None:
        while server.alive():
            await channel.pause(SIMULATION_SECONDS)
            _send_updates(channel, server, session)

    session.updates = ctx.background(every_five_seconds)
    return {
        "content": [
            text(
                f"Started simulated resource updated notifications for session {sid} at a 5 "
                "second pace. Client will receive updates for any resources the it is subscribed "
                "to."
            )
        ]
    }


# -- long-running operation ------------------------------------------------------------------------


def _progress_token(ctx: RequestContext) -> tuple[bool, JsonValue]:
    message = ctx.json_body if isinstance(ctx.json_body, dict) else {}
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict) and "progressToken" in meta:
        return True, meta["progressToken"]
    return False, None


async def trigger_long_running_operation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-long-running-operation``: ``steps`` waits of ``duration / steps`` seconds."""
    args, refusal = LONG_RUNNING.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    duration = js.as_number(args["duration"])
    steps = js.as_number(args["steps"])
    try:
        step = duration / steps
    except ZeroDivisionError:
        step = float("nan") if duration == 0 else float("inf")
    wants_progress, token = _progress_token(ctx)
    i = 1
    while i < steps + 1:
        await ctx.pause(js.timer_seconds(step))
        await asyncio.sleep(0)
        if wants_progress:
            ctx.notify(
                "notifications/progress",
                {"progress": i, "total": js.json_number(steps), "progressToken": token},
            )
        i += 1
    return {
        "content": [
            text(
                f"Long running operation completed. Duration: {js.js_number(duration)} seconds, "
                f"Steps: {js.js_number(steps)}."
            )
        ]
    }


# -- roots -----------------------------------------------------------------------------------------


async def sync_roots(ctx: RequestContext, session: McpSession, channel: Channel) -> None:
    """``syncRoots``: ask the client for its roots (on its GET stream) unless already known."""
    if "roots" not in ctx.client_capabilities or session.roots is not None:
        return
    transport_of(ctx).roots_synced.add(session.id)
    await request_roots(session, channel, ctx)


async def request_roots(session: McpSession, channel: Channel, ctx: RequestContext) -> None:
    """``requestRoots``: the client's roots, then an ``info`` log message about them.

    The request goes on the GET stream: without one open the real server's request is lost and
    times out after a minute (the fake fails at once and takes that minute unless fast). A
    client that refuses it is answered at once, without a log message (recording 000469).
    """
    started = time.monotonic()
    try:
        answer = await channel.ask("roots/list", None, within=REQUEST_TIMEOUT_S)
        roots = list_roots_result(answer)
    except ClientError as error:
        if error.code == REQUEST_TIMED_OUT:  # lost (no GET stream) or never answered
            await ctx.pause(max(0.0, REQUEST_TIMEOUT_S - (time.monotonic() - started)))
        return
    except InvalidAnswerError:
        return
    session.roots = roots
    log_message(
        channel,
        session,
        "info",
        f"Roots updated: {len(roots)} root(s) received from client",
        logger="everything-server",
    )


async def get_roots_list(ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """``get-roots-list``: the client's roots (fetched once per session)."""
    session = session_of(ctx)
    assert ctx.channel is not None  # noqa: S101 - every MCP call has one
    await sync_roots(ctx, session, ctx.channel)
    roots = session.roots
    if not roots:
        return {
            "content": [
                text(
                    "The client supports roots but no roots are currently configured.\n\n"
                    "This could mean:\n"
                    "1. The client hasn't provided any roots yet\n"
                    "2. The client provided an empty roots list\n"
                    "3. The roots configuration is still being loaded"
                )
            ]
        }
    listed = "\n\n".join(
        f"{index + 1}. {_root_name(root)}\n   URI: {_root_uri(root)}"
        for index, root in enumerate(roots)
    )
    return {
        "content": [
            text(
                f"Current MCP Roots ({len(roots)} total):\n\n{listed}\n\n"
                "Note: This server demonstrates the roots protocol capability but doesn't "
                "actually access files. The roots are provided by the MCP client and can be used "
                "by servers that need file system access."
            )
        ]
    }


def _root_uri(root: JsonValue) -> str:
    return js.js_string(root.get("uri") if isinstance(root, dict) else None)


def _root_name(root: JsonValue) -> str:
    name = root.get("name") if isinstance(root, dict) else None
    return js.js_string(name) if js.js_truthy(name) else "Unnamed Root"


# -- sampling --------------------------------------------------------------------------------------


def _sampling_request(tool: str, prompt: str, max_tokens: JsonValue) -> JsonObject:
    return {
        "messages": [{"role": "user", "content": text(f"Resource {tool} context: {prompt}")}],
        "systemPrompt": SYSTEM_PROMPT,
        "maxTokens": js.json_number(js.as_number(max_tokens)),
        "temperature": js.json_number(0.7),
    }


async def trigger_sampling_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-sampling-request``: ask the client's LLM, show what it answered."""
    args, refusal = SAMPLING.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    request = _sampling_request("trigger-sampling-request", str(args["prompt"]), args["maxTokens"])
    try:
        answer = await ctx.ask_client("sampling/createMessage", request, within=REQUEST_TIMEOUT_S)
        result = create_message_result(answer)
    except ClientError as error:
        return tool_error(client_error_text(error))
    except InvalidAnswerError as error:
        return tool_error(str(error))
    return {"content": [text(f"LLM sampling result: \n{js.js_stringify(result, 2)}")]}


# -- elicitation -----------------------------------------------------------------------------------


async def trigger_elicitation_request(ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """``trigger-elicitation-request``: ask the user to fill the demo form."""
    try:
        answer = await ctx.ask_client(
            "elicitation/create", ELICITATION_FORM, within=ELICITATION_TIMEOUT_S
        )
        result = elicit_result(answer)
    except ClientError as error:
        return tool_error(client_error_text(error))
    except InvalidAnswerError as error:
        return tool_error(str(error))
    content: list[JsonValue] = []
    action, data = result.get("action"), result.get("content")
    if action == "accept" and js.js_truthy(data):
        content.append(text("✅ User provided the requested information!"))
        user = data if isinstance(data, dict) else {}
        lines = [
            line
            for line in (
                _line("Name", user, "name"),
                _line("Agreed to terms", user, "check", defined=True),
                _line("Favorite Color", user, "color"),
                _line("Email", user, "email"),
                _line("Homepage", user, "homepage"),
                _line("Birthdate", user, "birthdate"),
                _line("Favorite Integer", user, "integer", defined=True),
                _line("Favorite Number", user, "number", defined=True),
                _line("Pet Type", user, "petType"),
            )
            if line is not None
        ]
        content.append(text("User inputs:\n" + "\n".join(lines)))
    elif action == "decline":
        content.append(text("❌ User declined to provide the requested information."))
    elif action == "cancel":
        content.append(text("⚠️ User cancelled the elicitation dialog."))
    content.append(text(f"\nRaw result: {js.js_stringify(result, 2)}"))
    return {"content": content}


def _line(label: str, data: JsonObject, key: str, *, defined: bool = False) -> str | None:
    """``if (userData.key) lines.push(...)`` (``defined``: ``!== undefined`` instead)."""
    if key not in data or not (defined or js.js_truthy(data[key])):
        return None
    return f"- {label}: {js.js_string(data[key])}"


async def trigger_url_elicitation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """``trigger-url-elicitation``: a URL-mode elicitation, by request or by -32042 error."""
    args, refusal = URL_ELICITATION.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    url, message = str(args["url"]), str(args["message"])
    requested = args.get("elicitationId")
    elicitation_id = str(requested) if requested is not None else str(uuid.uuid4())
    session = session_of(ctx)
    key = f"{ctx.mcp_session or 'default'}\0{url}\0{requested if requested is not None else ''}"
    if args["errorPath"]:
        if key in session.error_path_issued:  # the retry of a satisfied prerequisite proceeds
            session.error_path_issued.discard(key)
        else:
            session.error_path_issued.add(key)
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
    params: JsonObject = {
        "mode": "url",
        "url": url,
        "message": message,
        "elicitationId": elicitation_id,
    }
    try:
        answer = await ctx.ask_client("elicitation/create", params, within=ELICITATION_TIMEOUT_S)
        result = elicit_result(answer)
    except ClientError as error:
        return tool_error(client_error_text(error))
    except InvalidAnswerError as error:
        return tool_error(str(error))
    content: list[JsonValue] = []
    action = result.get("action")
    if action == "accept":
        content.append(
            text(
                "✅ User completed the URL elicitation flow.\n"
                f"Elicitation ID: {elicitation_id}\nURL: {url}"
            )
        )
    elif action == "decline":
        content.append(
            text(f"❌ User declined to open the URL (Elicitation ID: {elicitation_id}).")
        )
    elif action == "cancel":
        content.append(
            text(f"⚠️ User cancelled the URL elicitation (Elicitation ID: {elicitation_id}).")
        )
    content.append(text(f"\nRaw result: {js.js_stringify(result, 2)}"))
    return {"content": content}


# -- client-side tasks (async sampling and elicitation) --------------------------------------------


async def _poll_client_task(
    ctx: RequestContext, task: JsonObject, limit: int, *, keep: Callable[[int, str], bool]
) -> tuple[str, str | None, int, list[str]]:
    """Poll the client's task every second: final status, its message, polls made, the log."""
    task_id = str(task.get("taskId"))
    log = [f"Task created: {task_id}"]
    status, status_message = str(task.get("status")), None
    attempts = 0
    while status not in {"completed", "failed", "cancelled"} and attempts < limit:
        await ctx.pause(ASYNC_POLL_SECONDS)
        attempts += 1
        polled = poll_result(
            await ctx.ask_client("tasks/get", {"taskId": task_id}, within=REQUEST_TIMEOUT_S)
        )
        status = js.js_string(polled.get("status"))
        message = polled.get("statusMessage")
        status_message = str(message) if isinstance(message, str) else None
        if keep(attempts, status):
            log.append(
                f"Poll {attempts}: {status}{f' - {status_message}' if status_message else ''}"
            )
    return status, status_message, attempts, log


async def trigger_sampling_request_async(  # noqa: PLR0911 - one answer per outcome
    ctx: RequestContext, arguments: JsonObject
) -> JsonValue:
    """``trigger-sampling-request-async``: a sampling request the client may run as a task."""
    args, refusal = SAMPLING_ASYNC.parse(arguments)
    if refusal is not None:
        return tool_error(refusal)
    request: JsonObject = {
        "task": {"ttl": 300000},
        **_sampling_request(
            "trigger-sampling-request-async", str(args["prompt"]), args["maxTokens"]
        ),
    }
    sync_fields = (
        ("role", "string", True),
        ("content", "any", True),  # z.any() is required: "expected nonoptional" (000478)
        ("model", "string", True),
        ("stopReason", "string", False),
    )
    try:
        answer = task_or(
            await ctx.ask_client("sampling/createMessage", request, within=REQUEST_TIMEOUT_S),
            sync_fields,
        )
        task = answer.get("task")
        if not isinstance(task, dict):
            return {
                "content": [
                    text(f"[SYNC] Client executed synchronously:\n{js.js_stringify(answer, 2)}")
                ]
            }
        status, message, attempts, log = await _poll_client_task(
            ctx, task, ASYNC_SAMPLING_POLLS, keep=lambda _attempt, _status: True
        )
        progress = "\n".join(log)
        if attempts >= ASYNC_SAMPLING_POLLS:
            return {
                "content": [
                    text(
                        f"[TIMEOUT] Task timed out after {ASYNC_SAMPLING_POLLS} poll attempts\n\n"
                        f"Progress:\n{progress}"
                    )
                ]
            }
        if status in {"failed", "cancelled"}:
            return {
                "content": [
                    text(f"[{status.upper()}] {message or 'No message'}\n\nProgress:\n{progress}")
                ]
            }
        result = await ctx.ask_client(
            "tasks/result", {"taskId": str(task.get("taskId"))}, within=REQUEST_TIMEOUT_S
        )
    except ClientError as error:
        return tool_error(client_error_text(error))
    except InvalidAnswerError as error:
        return tool_error(str(error))
    return {
        "content": [
            text(
                "[COMPLETED] Async sampling completed!\n\n"
                f"**Progress:**\n{progress}\n\n**Result:**\n{js.js_stringify(result, 2)}"
            )
        ]
    }


async def trigger_elicitation_request_async(
    ctx: RequestContext, _arguments: JsonObject
) -> JsonValue:
    """``trigger-elicitation-request-async``: an elicitation the client may run as a task."""
    request: JsonObject = {"task": {"ttl": 600000}, **ASYNC_ELICITATION_FORM}
    try:
        answer = task_or(
            await ctx.ask_client("elicitation/create", request, within=REQUEST_TIMEOUT_S),
            (("action", "string", True), ("content", "any", False)),
        )
        task = answer.get("task")
        if not isinstance(task, dict):
            return {
                "content": [
                    text(f"[SYNC] Client executed synchronously:\n{js.js_stringify(answer, 2)}")
                ]
            }
        status, message, attempts, log = await _poll_client_task(
            ctx,
            task,
            ASYNC_ELICITATION_POLLS,
            keep=lambda attempt, polled: (
                attempt == 1 or attempt % 10 == 0 or polled != "input_required"
            ),
        )
        progress = "\n".join(log)
        if attempts >= ASYNC_ELICITATION_POLLS:
            return {
                "content": [
                    text(
                        f"[TIMEOUT] Task timed out after {ASYNC_ELICITATION_POLLS} poll "
                        f"attempts\n\nProgress:\n{progress}"
                    )
                ]
            }
        if status in {"failed", "cancelled"}:
            return {
                "content": [
                    text(f"[{status.upper()}] {message or 'No message'}\n\nProgress:\n{progress}")
                ]
            }
        result = await ctx.ask_client(
            "tasks/result", {"taskId": str(task.get("taskId"))}, within=REQUEST_TIMEOUT_S
        )
    except ClientError as error:
        return tool_error(client_error_text(error))
    except InvalidAnswerError as error:
        return tool_error(str(error))
    content: list[JsonValue] = []
    action = result.get("action") if isinstance(result, dict) else None
    data = result.get("content") if isinstance(result, dict) else None
    if action == "accept" and js.js_truthy(data):
        content.append(text("[COMPLETED] User provided the requested information!"))
        user = data if isinstance(data, dict) else {}
        lines = [
            line
            for line in (
                _line("Name", user, "name"),
                _line("Favorite Color", user, "favoriteColor"),
                _line("Agreed to terms", user, "agreeToTerms", defined=True),
            )
            if line is not None
        ]
        content.append(text("User inputs:\n" + "\n".join(lines)))
    elif action == "decline":
        content.append(text("[DECLINED] User declined to provide the requested information."))
    elif action == "cancel":
        content.append(text("[CANCELLED] User cancelled the elicitation dialog."))
    content.append(text(f"\nProgress:\n{progress}\n\nRaw result: {js.js_stringify(result, 2)}"))
    return {"content": content}


async def simulate_research_query(_ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """``simulate-research-query`` called without ``task``: it requires task augmentation.

    Task-augmented calls never reach this handler: ``handlers.call_tool`` creates the task.
    """
    return tool_error(
        "MCP error -32601: Tool simulate-research-query requires task augmentation "
        "(taskSupport: 'required')"
    )


TOOLS: Final[dict[str, ToolHandler]] = {
    "echo": echo,
    "get-annotated-message": get_annotated_message,
    "get-tiny-image": get_tiny_image,
    "get-resource-links": get_resource_links,
    "get-resource-reference": get_resource_reference,
    "get-structured-content": get_structured_content,
    "get-sum": get_sum,
    "gzip-file-as-resource": gzip_file_as_resource,
    "toggle-simulated-logging": toggle_simulated_logging,
    "toggle-subscriber-updates": toggle_subscriber_updates,
    "trigger-long-running-operation": trigger_long_running_operation,
    "get-roots-list": get_roots_list,
    "trigger-sampling-request": trigger_sampling_request,
    "trigger-elicitation-request": trigger_elicitation_request,
    "trigger-url-elicitation": trigger_url_elicitation,
    "simulate-research-query": simulate_research_query,
    "trigger-sampling-request-async": trigger_sampling_request_async,
    "trigger-elicitation-request-async": trigger_elicitation_request_async,
}
"""Tool name -> handler (``get-env`` replays its recording: the server's environment)."""
