"""Resources and what a client turns on: reads, listings, subscriptions, logging, gzip files.

Static documents are served as recorded; the two templates fabricate resources from their id and
the clock; ``gzip-file-as-resource`` adds resources to the calling client's session (and to its
``resources/list``), notifying it with ``notifications/resources/list_changed`` as the SDK does.
"""

from __future__ import annotations

import base64
import binascii
import copy
import re
import urllib.parse
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fauxcall.runtime import RpcError

from fauxcall_everything import jsfmt, whatwg, zod
from fauxcall_everything.common import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    LEVELS,
    NS_PER_S,
    ClientState,
    SessionResource,
    ToolError,
    check_cursor,
    client_state,
    log_message,
    mcp_error_text,
    now_ns,
    protocol_error,
    validated_tool,
)
from fauxcall_everything.tools import BLOB_URI, TEXT_URI, blob_resource, text_resource

if TYPE_CHECKING:
    from collections.abc import Callable

    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.runtime.app import FakeRuntime
    from fauxcall.types import JsonObject, JsonValue

SESSION_URI: Final = "demo://resource/session/"
STATIC_URI: Final = "demo://resource/static/document/"
DEFAULT_GZIP_SOURCE: Final = (
    "https://raw.githubusercontent.com/modelcontextprotocol/servers/refs/heads/main/README.md"
)
GZIP_OS_CODE: Final = 0x13
"""The OS byte of the real server's gzip headers (its zlib build), whatever this host writes."""
_GZIP_OS_OFFSET: Final = 9
PACE_SECONDS: Final = 5.0
"""Simulated logging and resource updates tick every 5 seconds."""
_TEMPLATE_VALUE: Final = re.compile(r"[^/,]+")
_LOG_MESSAGES: Final = {
    "debug": "Debug-level message",
    "info": "Info-level message",
    "notice": "Notice-level message",
    "warning": "Warning-level message",
    "error": "Error-level message",
    "critical": "Critical-level message",
    "alert": "Alert level-message",
    "emergency": "Emergency-level message",
}


# -- resources/read, resources/list -----------------------------------------------------------


def _recorded_read(ctx: RequestContext, uri: str) -> JsonValue:
    """The recorded answer for a static document."""
    for exchange in ctx.runtime.recordings.for_endpoint(f"MCP resources/read:{uri}"):
        reply = exchange.response.body.parsed() if exchange.response else None
        result = reply.get("result") if isinstance(reply, dict) else None
        if isinstance(result, dict):
            return copy.deepcopy(result)
    return None


def _static_uris(ctx: RequestContext) -> list[str]:
    mcp = ctx.model.mcp
    resources = mcp.resources if mcp is not None else []
    return [str(r["uri"]) for r in resources if isinstance(r, dict) and "uri" in r]


def _template_id(href: str) -> tuple[str, str] | None:
    """``(type, raw id)`` when the URI matches one of the two templates."""
    for kind, base in (("Text", TEXT_URI), ("Blob", BLOB_URI)):
        if href.startswith(base) and _TEMPLATE_VALUE.fullmatch(href[len(base) :]):
            return kind, href[len(base) :]
    return None


def _uri_param(params: JsonObject) -> str:
    uri = params.get("uri", jsfmt.UNDEFINED)
    if not isinstance(uri, str):
        raise protocol_error([zod.invalid_type("string", uri, ["params", "uri"])])
    return uri


async def read_resource(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/read``: a registered resource, else a template's, else ``not found``."""
    uri = _uri_param(params)
    try:
        href = whatwg.href(uri)
    except whatwg.InvalidUrlError as error:
        raise RpcError(INTERNAL_ERROR, "Invalid URL") from error
    if href in _static_uris(ctx):
        recorded = _recorded_read(ctx, href)
        if recorded is not None:
            return recorded
    resource = client_state(ctx).resources.get(href)
    if resource is not None:
        return {"contents": [{"uri": href, "mimeType": resource.mime_type, "blob": resource.blob}]}
    matched = _template_id(href)
    if matched is not None:
        kind, raw = matched
        resource_id = jsfmt.to_number(raw)
        if not jsfmt.is_positive_integer(resource_id):
            raise RpcError(INTERNAL_ERROR, f"Unknown resource: {href}")
        make = text_resource if kind == "Text" else blob_resource
        return {"contents": [make(href, resource_id, ctx.now_ns)]}
    raise RpcError(INVALID_PARAMS, mcp_error_text(INVALID_PARAMS, f"Resource {href} not found"))


async def list_resources(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/list``: the documents, then the client's gzip resources (cursor ignored)."""
    check_cursor(params)
    mcp = ctx.model.mcp
    listed: list[JsonValue] = copy.deepcopy(list(mcp.resources)) if mcp is not None else []
    listed += [
        {"uri": r.uri, "name": r.name, "mimeType": r.mime_type}
        for r in client_state(ctx).resources.values()
    ]
    return {"resources": listed}


# -- logging -------------------------------------------------------------------------------------


async def set_level(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``logging/setLevel``: later log messages below it are not sent."""
    level = params.get("level")
    if not isinstance(level, str) or level not in LEVELS:
        raise protocol_error([zod.invalid_value(LEVELS, ["params", "level"])])
    client_state(ctx).level = level
    return {}


async def _every_pace(ticker: Ticker, beat: Callable[[], None]) -> None:
    """``beat()`` now and every 5 seconds after, on the session's clock (``setInterval``).

    Ticks are due at fixed times from the start: a clock moved ahead sends every tick it passed.
    """
    due = ticker.now()
    while True:
        beat()
        due += int(PACE_SECONDS * NS_PER_S)
        left = due - ticker.now()
        if left > 0:
            await ticker.runtime.wait(ticker.session, left / NS_PER_S)


@dataclass(frozen=True, slots=True)
class Ticker:
    """The clock periodic work follows: the fake's runtime and the evaluation session."""

    runtime: FakeRuntime
    session: str

    def now(self) -> int:
        """The session's clock."""
        return now_ns(self.runtime, self.session)


def _ticker(ctx: RequestContext) -> Ticker:
    return Ticker(ctx.runtime, ctx.session)


async def _simulated_logging(
    channel: Channel, ticker: Ticker, state: ClientState, seed: int
) -> None:
    """A random-level log message now and every 5 seconds (the client's level applies)."""
    import random  # noqa: PLC0415 - only the simulation needs it

    pick = random.Random(seed)

    def beat() -> None:
        level = pick.choice(LEVELS)
        message = f"{_LOG_MESSAGES[level]} - SessionId {state.session}"
        log_message(channel.push, state, level, message)

    await _every_pace(ticker, beat)


@validated_tool("toggle-simulated-logging", [])
async def toggle_simulated_logging(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """Start (or stop) random-level log messages for this client, every 5 seconds."""
    del args
    state = client_state(ctx)
    if state.logging is not None:
        state.logging.cancel()
        state.logging = None
        message = f"Stopped simulated logging for session {state.session}"
    else:
        seed = ctx.rng.randrange(2**32)
        ticker = _ticker(ctx)
        state.logging = ctx.background(
            lambda channel: _simulated_logging(channel, ticker, state, seed)
        )
        message = (
            f"Started simulated, random-leveled logging for session {state.session} at a 5 second "
            "pace. Client's selected logging level will be respected. If an interval elapses and "
            "the message to be sent is below the selected level, it will not be sent. Thus at "
            "higher chosen logging levels, messages should arrive further apart. "
        )
    return {"content": [{"type": "text", "text": message}]}


# -- subscriptions -----------------------------------------------------------------------------


async def subscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/subscribe``: acknowledged with a log message; any URI is accepted."""
    uri = _uri_param(params)
    state = client_state(ctx)
    log_message(
        ctx.push,
        state,
        "info",
        f"Received Subscribe Resource request for URI: {uri} from session {state.session}",
    )
    state.subscriptions[uri] = True
    return {}


async def unsubscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/unsubscribe``: acknowledged with a log message."""
    uri = _uri_param(params)
    state = client_state(ctx)
    log_message(
        ctx.push,
        state,
        "info",
        f"Received Unsubscribe Resource request: {uri} from session {state.session}",
    )
    if uri in state.subscriptions:
        state.subscriptions[uri] = False
    return {}


async def _resource_updates(channel: Channel, ticker: Ticker, state: ClientState) -> None:
    """``notifications/resources/updated`` for every subscribed URI, now and every 5 seconds."""

    def beat() -> None:
        for uri, subscribed in list(state.subscriptions.items()):
            if subscribed:
                channel.push("notifications/resources/updated", {"uri": uri})

    await _every_pace(ticker, beat)


@validated_tool("toggle-subscriber-updates", [])
async def toggle_subscriber_updates(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """Start (or stop) update notifications for this client's subscriptions, every 5 seconds."""
    del args
    state = client_state(ctx)
    if state.updates is not None:
        state.updates.cancel()
        state.updates = None
        message = f"Stopped simulated resource updates for session {state.session}"
    else:
        ticker = _ticker(ctx)
        state.updates = ctx.background(lambda channel: _resource_updates(channel, ticker, state))
        message = (
            f"Started simulated resource updated notifications for session {state.session} at a "
            "5 second pace. Client will receive updates for any resources the it is subscribed to."
        )
    return {"content": [{"type": "text", "text": message}]}


# -- gzip-file-as-resource -----------------------------------------------------------------------


def gzip_bytes(raw: bytes) -> bytes:
    """``zlib.gzipSync(raw)`` as the real server writes it."""
    packer = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, 31)
    packed = bytearray(packer.compress(raw) + packer.flush())
    packed[_GZIP_OS_OFFSET] = GZIP_OS_CODE
    return bytes(packed)


def _forgiving_base64(data: bytes) -> bytes | None:
    """The fetch standard's forgiving-base64 decode (``None``: failure)."""
    text = re.sub(rb"[\t\n\f\r ]", b"", data)
    if len(text) % 4 == 0:
        text = text.removesuffix(b"==") if text.endswith(b"==") else text.removesuffix(b"=")
    if len(text) % 4 == 1 or re.search(rb"[^A-Za-z0-9+/]", text):
        return None
    try:
        return base64.b64decode(text + b"=" * (-len(text) % 4))
    except binascii.Error:  # pragma: no cover - the checks above leave nothing invalid
        return None


def data_url_body(href: str) -> bytes | None:
    """What ``fetch`` reads from a ``data:`` URL (``None``: the fetch fails)."""
    rest = href.split("#", 1)[0][len("data:") :]
    if "," not in rest:
        return None
    mime, encoded = rest.split(",", 1)
    body = urllib.parse.unquote_to_bytes(encoded)
    if re.search(r";[\x20\t\n\r\f]*base64[\x20\t\n\r\f]*$", mime, re.IGNORECASE):
        return _forgiving_base64(body)
    return body


def _recorded_fetch(ctx: RequestContext, url: str) -> tuple[bytes, float] | None:
    """The bytes the real server fetched from ``url`` when recorded, and how long that took."""
    for exchange in ctx.runtime.recordings.for_endpoint("MCP tools/call:gzip-file-as-resource"):
        sent = exchange.request.body.parsed()
        params = sent.get("params") if isinstance(sent, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if not isinstance(arguments, dict) or arguments.get("data", DEFAULT_GZIP_SOURCE) != url:
            continue
        reply = exchange.response.body.parsed() if exchange.response else None
        result = reply.get("result") if isinstance(reply, dict) else None
        content = result.get("content") if isinstance(result, dict) else None
        for block in content if isinstance(content, list) else []:
            resource = block.get("resource") if isinstance(block, dict) else None
            if isinstance(resource, dict) and isinstance(resource.get("blob"), str):
                raw = zlib.decompress(base64.b64decode(str(resource["blob"])), 31)
                return raw, exchange.elapsed_ms / 1000
    return None


def _register(ctx: RequestContext, state: ClientState, resource: SessionResource) -> None:
    """``registerSessionResource``: replace the resource of the same URI, then add it last.

    The real server keeps one registry for all its sessions: the resource it replaces may be
    another session's, which loses it (and is told its list changed).
    """
    owners: dict[str, ClientState] = ctx.state.setdefault("gzip_resource_owners", {})
    previous = owners.get(resource.uri, state)
    if resource.uri in previous.resources:
        del previous.resources[resource.uri]
        if previous is state:
            ctx.push("notifications/resources/list_changed")
        elif previous.channel is not None:
            previous.channel.push("notifications/resources/list_changed")
    state.resources[resource.uri] = resource
    owners[resource.uri] = state
    ctx.push("notifications/resources/list_changed")


@validated_tool(
    "gzip-file-as-resource",
    [
        zod.Field("name", "string", default="README.md.gz"),
        zod.Field("data", "url", default=DEFAULT_GZIP_SOURCE),
        zod.Field(
            "outputType", "enum", values=("resourceLink", "resource"), default="resourceLink"
        ),
    ],
)
async def gzip_file_as_resource(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """Compress a data: URL (or a file the real server fetched when recorded) into a resource."""
    name, source = str(args["name"]), str(args["data"])
    scheme = whatwg.parse(source).scheme
    if scheme not in {"http", "https", "data"}:
        raise ToolError(
            f"Error processing file {source}: Unsupported URL protocol for {source}. Only http, "
            "https, and data URLs are supported."
        )
    if scheme == "data":
        raw = data_url_body(whatwg.href(source))
    else:
        fetched = _recorded_fetch(ctx, source)
        raw = None
        if fetched is not None:
            raw, seconds = fetched
            await ctx.pause(seconds)
    if raw is None:
        raise ToolError("fetch failed")  # what the real server says when the fetch fails
    uri = SESSION_URI + name
    blob = base64.b64encode(gzip_bytes(raw)).decode("ascii")
    _register(ctx, client_state(ctx), SessionResource(uri, name, "application/gzip", blob))
    if args["outputType"] == "resource":
        resource: JsonObject = {"uri": uri, "mimeType": "application/gzip", "blob": blob}
        return {"content": [{"type": "resource", "resource": resource}]}
    link: JsonObject = {"name": name, "uri": uri, "mimeType": "application/gzip"}
    return {"content": [{**link, "type": "resource_link"}]}
