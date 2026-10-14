"""The Everything server's tools, answered the way the real one answers them.

Every behavior here was recorded (``data/recordings``); the server's published source explained
the rules behind the recordings (step timings, which fields its texts print, error wording).
"""

from __future__ import annotations

import base64
import binascii
import math
import time
import urllib.parse
import uuid
import zlib
from typing import TYPE_CHECKING, Any, Final

from fauxcall.runtime import ClientError, RpcError

from fauxcall_everything import jsfmt
from fauxcall_everything.jsfmt import Field
from fauxcall_everything.state import (
    ResearchTask,
    SessionResource,
    assets,
    local_time,
    log_allowed,
    mcp_session,
    resource_owners,
    session_id,
    subscriptions,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.types import JsonObject, JsonValue

    ToolHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]

TEXT_BASE: Final = "demo://resource/dynamic/text"
BLOB_BASE: Final = "demo://resource/dynamic/blob"
SESSION_BASE: Final = "demo://resource/session/"
README_URL: Final = (
    "https://raw.githubusercontent.com/modelcontextprotocol/servers/refs/heads/main/README.md"
)
SAMPLING_TIMEOUT: Final = 60.0
"""The SDK's default request timeout."""
ELICITATION_TIMEOUT: Final = 600.0
"""The server waits 10 minutes for a person to answer an elicitation."""
UPDATE_PERIOD: Final = 5.0
POLL_INTERVAL: Final = 1.0
SAMPLING_POLLS: Final = 60
ELICITATION_POLLS: Final = 600
NODE_GZIP_OS: Final = 0x13
"""The OS byte Node's zlib writes in gzip headers (the real blobs carry it)."""
GZIP_OS_OFFSET: Final = 9
MAX_TIMER_SECONDS: Final = (2**31 - 1) / 1000
REQUEST_TIMEOUT: Final = -32001
UNSENT_SECONDS: Final = 0.05
"""A request refused this fast never reached the client: it had no stream open."""


def text(value: str) -> JsonObject:
    """A text content block."""
    return {"type": "text", "text": value}


def failure(message: str) -> JsonObject:
    """A tool result reporting an error the tool threw (the SDK's isError result)."""
    return {"content": [text(message)], "isError": True}


def invalid(tool: str, issues: list[str]) -> JsonObject:
    """The SDK's answer to arguments that break the tool's zod schema."""
    detail = "\n".join(issues)
    return failure(
        f"MCP error -32602: Input validation error: Invalid arguments for tool {tool}: {detail}"
    )


def client_failure(error: RpcError) -> JsonObject:
    """A request to the client that failed, as the tool reports it."""
    return failure(f"MCP error {error.code}: {error.message}")


def resource_uri(kind: str, resource_id: float) -> str:
    """``new URL(`${base}/${id}`)`` for a dynamic resource."""
    return f"{TEXT_BASE if kind == 'Text' else BLOB_BASE}/{jsfmt.number(resource_id)}"


def dynamic_contents(ctx: RequestContext, uri: str, kind: str, resource_id: float) -> JsonObject:
    """The contents of a dynamic text or blob resource, stamped with the server's local time."""
    stamp = local_time(ctx.now_ns)
    shown = jsfmt.number(resource_id)
    if kind == "Text":
        body = f"Resource {shown}: This is a plaintext resource created at {stamp}"
        return {"uri": uri, "mimeType": "text/plain", "text": body}
    blob = f"Resource {shown}: This is a base64 blob created at {stamp}"
    encoded = base64.b64encode(blob.encode()).decode()
    return {"uri": uri, "mimeType": "text/plain", "blob": encoded}


# -- simple tools ----------------------------------------------------------------------------------


async def echo(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Echoes back the input string."""
    args, issues = jsfmt.check({"message": Field("string", required=True)}, arguments)
    if issues:
        return invalid("echo", issues)
    return {"content": [text(f"Echo: {args['message']}")]}


async def get_sum(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """The sum of two numbers, as JavaScript adds and prints them."""
    schema = {"a": Field("number", required=True), "b": Field("number", required=True)}
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("get-sum", issues)
    a, b = jsfmt.as_float(args["a"]), jsfmt.as_float(args["b"])
    total = a + b
    shown = (jsfmt.number(a), jsfmt.number(b), jsfmt.number(total))
    return {"content": [text(f"The sum of {shown[0]} and {shown[1]} is {shown[2]}.")]}


_ANNOTATED: Final[dict[str, tuple[str, JsonObject]]] = {
    "error": ("Error: Operation failed", {"audience": ["user", "assistant"], "priority": 1}),
    "success": ("Operation completed successfully", {"audience": ["user"], "priority": 0.7}),
    "debug": (
        "Debug: Cache hit ratio 0.95, latency 150ms",
        {"audience": ["assistant"], "priority": 0.3},
    ),
}


async def get_annotated_message(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """A message whose annotations depend on its type, optionally with an annotated image."""
    schema = {
        "messageType": Field("enum", required=True, options=("error", "success", "debug")),
        "includeImage": Field("boolean"),
    }
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("get-annotated-message", issues)
    message, annotations = _ANNOTATED[str(args["messageType"])]
    content: list[JsonValue] = [{"type": "text", "text": message, "annotations": annotations}]
    if args.get("includeImage") is True:
        content.append(
            {
                "type": "image",
                "data": assets()["tiny_image"],
                "mimeType": "image/png",
                "annotations": {"audience": ["user"], "priority": 0.5},
            }
        )
    return {"content": content}


async def get_env(_ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """The server process's environment, as recorded (it was started with an almost empty one)."""
    return {"content": [text(str(assets()["env_text"]))]}


async def get_tiny_image(_ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """The MCP logo, between two captions."""
    image = {"type": "image", "data": assets()["tiny_image"], "mimeType": "image/png"}
    return {
        "content": [
            text("Here's the image you requested:"),
            image,
            text("The image above is the MCP logo."),
        ]
    }


_WEATHER: Final[dict[str, JsonObject]] = {
    "New York": {"temperature": 33, "conditions": "Cloudy", "humidity": 82},
    "Chicago": {"temperature": 36, "conditions": "Light rain / drizzle", "humidity": 82},
    "Los Angeles": {"temperature": 73, "conditions": "Sunny / Clear", "humidity": 48},
}


async def get_structured_content(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Fixed weather per city, as text and as structured content."""
    schema = {"location": Field("enum", required=True, options=tuple(_WEATHER))}
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("get-structured-content", issues)
    weather = _WEATHER[str(args["location"])]
    compact = jsfmt.stringify(weather, indent=0).replace("\n", "").replace('": ', '":')
    return {"content": [text(compact)], "structuredContent": weather}


async def get_resource_links(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Links to dynamic resources, alternating blob (odd ids) and text (even ids)."""
    schema = {"count": Field("number", default=3, minimum=1, maximum=10)}
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("get-resource-links", issues)
    count = jsfmt.as_float(args["count"])
    content: list[JsonValue] = [
        text(
            f"Here are {jsfmt.number(count)} resource links to resources available in this server:"
        )
    ]
    resource_id = 1
    while resource_id <= count:
        kind = "Text" if resource_id % 2 == 0 else "Blob"
        content.append(
            {
                "name": f"{kind} Resource {resource_id}",
                "uri": resource_uri(kind, resource_id),
                "description": f"Resource {resource_id}: plaintext resource",
                "mimeType": "text/plain",
                "type": "resource_link",
            }
        )
        resource_id += 1
    return {"content": content}


async def get_resource_reference(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """An embedded dynamic resource and its URI."""
    schema = {
        "resourceType": Field("enum", default="Text", options=("Text", "Blob")),
        "resourceId": Field("number", default=1),
    }
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("get-resource-reference", issues)
    resource_id = jsfmt.as_float(args["resourceId"])
    if not jsfmt.is_positive_integer(resource_id):
        shown = jsfmt.number(resource_id)
        return failure(f"Invalid resourceId: {shown}. Must be a finite positive integer.")
    kind = str(args["resourceType"])
    uri = resource_uri(kind, resource_id)
    shown = jsfmt.number(resource_id)
    return {
        "content": [
            text(f"Returning resource reference for Resource {shown}:"),
            {"type": "resource", "resource": dynamic_contents(ctx, uri, kind, resource_id)},
            text(f"You can access this resource using the URI: {uri}"),
        ]
    }


# -- gzip ------------------------------------------------------------------------------------------


def node_gzip(data: bytes) -> str:
    """``gzipSync(data).toString('base64')`` as Node writes it (same deflate, OS byte 0x13)."""
    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
    out = bytearray(compressor.compress(data) + compressor.flush())
    out[GZIP_OS_OFFSET] = NODE_GZIP_OS
    return base64.b64encode(bytes(out)).decode()


def fetch(url: str) -> bytes | None:
    """What ``fetch(url)`` gives the real server, without the network.

    ``data:`` URLs are decoded as fetch decodes them; the default README answers with the bytes
    recorded from it; any other URL fails as the real server's fetch does with no network.
    """
    if url.lower().startswith("data:"):
        header, comma, payload = url[5:].partition(",")
        if not comma:
            return None
        raw = urllib.parse.unquote_to_bytes(payload)
        if header.strip().lower().endswith(";base64"):
            cleaned = b"".join(raw.split())
            try:
                return base64.b64decode(cleaned + b"=" * (-len(cleaned) % 4), validate=True)
            except (binascii.Error, ValueError):
                return None
        return raw
    if url == README_URL:
        return base64.b64decode(str(assets()["readme_base64"]))
    return None


def register_resource(ctx: RequestContext, resource: SessionResource) -> None:
    """Register a session resource, replacing one with the same URI (as the server does)."""
    session = mcp_session(ctx)
    owners = resource_owners(ctx)
    previous_owner = owners.get(resource.uri)
    if previous_owner is not None:
        sessions = ctx.state.get("mcp_sessions", {})
        holder = sessions.get(previous_owner)
        removed = holder is not None and holder.resources.pop(resource.uri, None) is not None
        if removed and previous_owner == session_id(ctx):
            ctx.push("notifications/resources/list_changed")
    session.resources[resource.uri] = resource
    owners[resource.uri] = session_id(ctx)
    ctx.push("notifications/resources/list_changed")


async def gzip_file_as_resource(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Gzip a URL's content into a session resource; answer with a link or the resource."""
    schema = {
        "name": Field("string", default="README.md.gz"),
        "data": Field("url", default=README_URL),
        "outputType": Field("enum", default="resourceLink", options=("resourceLink", "resource")),
    }
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("gzip-file-as-resource", issues)
    name, data_uri = str(args["name"]), str(args["data"])
    scheme = data_uri.split(":", 1)[0].lower()
    if scheme not in {"http", "https", "data"}:
        return failure(
            f"Error processing file {data_uri}: Unsupported URL protocol for {data_uri}. "
            "Only http, https, and data URLs are supported."
        )
    content = fetch(data_uri)
    if content is None:
        return failure("fetch failed")
    blob = node_gzip(content)
    uri = SESSION_BASE + name
    register_resource(ctx, SessionResource(uri, name, "application/gzip", blob))
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


# -- toggles (pushes on the client's GET stream) ---------------------------------------------------

_LOG_MESSAGES: Final = [
    ("debug", "Debug-level message"),
    ("info", "Info-level message"),
    ("notice", "Notice-level message"),
    ("warning", "Warning-level message"),
    ("error", "Error-level message"),
    ("critical", "Critical-level message"),
    ("alert", "Alert level-message"),
    ("emergency", "Emergency-level message"),
]


def log(ctx: RequestContext, level: str, data: str, logger: str | None = None) -> None:
    """``server.sendLoggingMessage``: on the GET stream, if the client's level lets it through."""
    if not log_allowed(mcp_session(ctx), level):
        return
    params: JsonObject = {"level": level}
    if logger is not None:
        params["logger"] = logger
    params["data"] = data
    ctx.push("notifications/message", params)


async def toggle_simulated_logging(ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """Start (or stop) a random-leveled log message every 5 seconds."""
    session = mcp_session(ctx)
    sid = session_id(ctx)
    if session.logging_task is not None:
        session.logging_task.cancel()
        session.logging_task = None
        return {"content": [text(f"Stopped simulated logging for session {sid}")]}
    suffix = f" - SessionId {sid}" if sid else ""
    rng = ctx.rng

    async def run(channel: Channel) -> None:
        while True:
            level, message = _LOG_MESSAGES[rng.randrange(len(_LOG_MESSAGES))]
            if log_allowed(session, level):
                channel.push("notifications/message", {"level": level, "data": message + suffix})
            await channel.pause(UPDATE_PERIOD)

    session.logging_task = ctx.background(run)
    return {
        "content": [
            text(
                f"Started simulated, random-leveled logging for session {sid} at a 5 second "
                "pace. Client's selected logging level will be respected. If an interval elapses "
                "and the message to be sent is below the selected level, it will not be sent. "
                "Thus at higher chosen logging levels, messages should arrive further apart. "
            )
        ]
    }


async def toggle_subscriber_updates(ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """Start (or stop) resource-updated notifications every 5 seconds for subscribed URIs."""
    session = mcp_session(ctx)
    sid = session_id(ctx)
    if session.updates_task is not None:
        session.updates_task.cancel()
        session.updates_task = None
        return {"content": [text(f"Stopped simulated resource updates for session {sid}")]}
    subscribed = subscriptions(ctx)

    async def run(channel: Channel) -> None:
        while True:
            for uri, sessions in list(subscribed.items()):
                if sid in sessions:
                    channel.push("notifications/resources/updated", {"uri": uri})
            await channel.pause(UPDATE_PERIOD)

    session.updates_task = ctx.background(run)
    return {
        "content": [
            text(
                f"Started simulated resource updated notifications for session {sid} at a 5 "
                "second pace. Client will receive updates for any resources the it is "
                "subscribed to."
            )
        ]
    }


# -- long-running operation ---------------------------------------------------------------------


async def trigger_long_running_operation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Sleep ``duration`` seconds in ``steps`` steps, with a progress notification after each."""
    schema = {"duration": Field("number", default=10), "steps": Field("number", default=5)}
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("trigger-long-running-operation", issues)
    duration, steps = jsfmt.as_float(args["duration"]), jsfmt.as_float(args["steps"])
    step_seconds = duration / steps if steps else math.inf
    step = 1
    while step < steps + 1:
        # Node fires a timer longer than 2^31-1 ms after 1 ms
        await ctx.pause(max(step_seconds, 0.0) if step_seconds <= MAX_TIMER_SECONDS else 0.001)
        ctx.progress(step, steps)
        step += 1
    shown = (jsfmt.number(duration), jsfmt.number(steps))
    return {
        "content": [
            text(
                f"Long running operation completed. Duration: {shown[0]} seconds, "
                f"Steps: {shown[1]}."
            )
        ]
    }


# -- requests to the client ------------------------------------------------------------------------


def _issue(path: list[JsonValue], expected: str, got: str) -> JsonObject:
    return {
        "expected": expected,
        "code": "invalid_type",
        "path": path,
        "message": f"Invalid input: expected {expected}, received {got}",
    }


def _choice_issue(path: list[JsonValue], values: tuple[str, ...]) -> JsonObject:
    quoted = "|".join(f'"{v}"' for v in values)
    return {
        "code": "invalid_value",
        "values": list(values),
        "path": path,
        "message": f"Invalid option: expected one of {quoted}",
    }


_CONTENT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "text": ("text",),
    "image": ("data", "mimeType"),
    "audio": ("data", "mimeType"),
}


def _content_issues(content: JsonValue, *, present: bool) -> list[JsonValue]:
    """The discriminated union zod applies to of sampling content (text, image, audio)."""
    if not isinstance(content, dict):
        return [_issue(["content"], "object", jsfmt.received(content, present=present))]
    fields = (
        _CONTENT_FIELDS.get(str(content.get("type")))
        if isinstance(content.get("type"), str)
        else None
    )
    if fields is None:
        return [
            {
                "code": "invalid_union",
                "errors": [],
                "note": "No matching discriminator",
                "discriminator": "type",
                "options": list(_CONTENT_FIELDS),
                "path": ["content", "type"],
                "message": "Invalid discriminator value. Expected 'text' | 'image' | 'audio'",
            }
        ]
    return [
        _issue(
            ["content", name], "string", jsfmt.received(content.get(name), present=name in content)
        )
        for name in fields
        if not isinstance(content.get(name), str)
    ]


def _ordered(value: JsonObject, order: tuple[str, ...], *, extras: bool = True) -> JsonObject:
    found = {key: value[key] for key in order if key in value}
    if extras:
        found.update({key: item for key, item in value.items() if key not in found})
    return found


_CONTENT_ORDER: Final = ("type", "text", "data", "mimeType", "annotations", "_meta")


def sampling_result(reply: JsonValue) -> JsonObject:
    """``CreateMessageResultSchema.parse(reply)``: its key order, or zod's issues."""
    found = reply if isinstance(reply, dict) else {}
    problems: list[JsonValue] = []
    if not isinstance(found.get("model"), str):
        got = jsfmt.received(found.get("model"), present="model" in found)
        problems.append(_issue(["model"], "string", got))
    if found.get("role") not in {"user", "assistant"}:
        problems.append(_choice_issue(["role"], ("user", "assistant")))
    problems += _content_issues(found.get("content"), present="content" in found)
    if problems:
        raise RpcError(0, jsfmt.stringify(problems))
    parsed = _ordered(found, ("_meta", "model", "stopReason", "role", "content"))
    content = parsed.get("content")
    if isinstance(content, dict):
        parsed["content"] = _ordered(content, _CONTENT_ORDER)
    return parsed


async def trigger_sampling_request(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Ask the client's LLM, and report what it answered."""
    schema = {"prompt": Field("string", required=True), "maxTokens": Field("number", default=100)}
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("trigger-sampling-request", issues)
    request = _sampling_params(args, "trigger-sampling-request")
    try:
        reply = await ctx.ask_client("sampling/createMessage", request, within=SAMPLING_TIMEOUT)
        result = sampling_result(reply)
    except ClientError as error:
        return client_failure(error)
    except RpcError as error:
        return failure(error.message)
    return {"content": [text(f"LLM sampling result: \n{jsfmt.stringify(result)}")]}


def _sampling_params(args: JsonObject, tool: str, task: JsonObject | None = None) -> JsonObject:
    params: JsonObject = {} if task is None else {"task": task}
    params["messages"] = [
        {"role": "user", "content": text(f"Resource {tool} context: {args['prompt']}")}
    ]
    params["systemPrompt"] = "You are a helpful test server."
    params["maxTokens"] = args["maxTokens"]
    params["temperature"] = 0.7
    return params


ELICITATION_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "name": {"title": "String", "type": "string", "description": "Your full, legal name"},
        "check": {
            "title": "Boolean",
            "type": "boolean",
            "description": "Agree to the terms and conditions",
        },
        "firstLine": {
            "title": "String with default",
            "type": "string",
            "description": "Favorite first line of a story",
            "default": "It was a dark and stormy night.",
        },
        "email": {
            "title": "String with email format",
            "type": "string",
            "format": "email",
            "description": "Your email address (will be verified, and never shared with anyone "
            "else)",
        },
        "homepage": {
            "type": "string",
            "format": "uri",
            "title": "String with uri format",
            "description": "Portfolio / personal website",
        },
        "birthdate": {
            "title": "String with date format",
            "type": "string",
            "format": "date",
            "description": "Your date of birth",
        },
        "integer": {
            "title": "Integer",
            "type": "integer",
            "description": "Your favorite integer (do not give us your phone number, pin, or other "
            "sensitive info)",
            "minimum": 1,
            "maximum": 100,
            "default": 42,
        },
        "number": {
            "title": "Number in range 1-1000",
            "type": "number",
            "description": "Favorite number (there are no wrong answers)",
            "minimum": 0,
            "maximum": 1000,
            "default": 3.14,
        },
        "untitledSingleSelectEnum": {
            "type": "string",
            "title": "Untitled Single Select Enum",
            "description": "Choose your favorite friend",
            "enum": ["Monica", "Rachel", "Joey", "Chandler", "Ross", "Phoebe"],
            "default": "Monica",
        },
        "untitledMultipleSelectEnum": {
            "type": "array",
            "title": "Untitled Multiple Select Enum",
            "description": "Choose your favorite instruments",
            "minItems": 1,
            "maxItems": 3,
            "items": {"type": "string", "enum": ["Guitar", "Piano", "Violin", "Drums", "Bass"]},
            "default": ["Guitar"],
        },
        "titledSingleSelectEnum": {
            "type": "string",
            "title": "Titled Single Select Enum",
            "description": "Choose your favorite hero",
            "oneOf": [
                {"const": "hero-1", "title": "Superman"},
                {"const": "hero-2", "title": "Green Lantern"},
                {"const": "hero-3", "title": "Wonder Woman"},
            ],
            "default": "hero-1",
        },
        "titledMultipleSelectEnum": {
            "type": "array",
            "title": "Titled Multiple Select Enum",
            "description": "Choose your favorite types of fish",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "anyOf": [
                    {"const": "fish-1", "title": "Tuna"},
                    {"const": "fish-2", "title": "Salmon"},
                    {"const": "fish-3", "title": "Trout"},
                ]
            },
            "default": ["fish-1"],
        },
        "legacyTitledEnum": {
            "type": "string",
            "title": "Legacy Titled Single Select Enum",
            "description": "Choose your favorite type of pet",
            "enum": ["pet-1", "pet-2", "pet-3", "pet-4", "pet-5"],
            "enumNames": ["Cats", "Dogs", "Birds", "Fish", "Reptiles"],
            "default": "pet-1",
        },
    },
    "required": ["name"],
}

ASYNC_ELICITATION_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "name": {"title": "Your Name", "type": "string", "description": "Your full name"},
        "favoriteColor": {
            "title": "Favorite Color",
            "type": "string",
            "description": "What is your favorite color?",
            "enum": ["Red", "Blue", "Green", "Yellow", "Purple"],
        },
        "agreeToTerms": {
            "title": "Terms Agreement",
            "type": "boolean",
            "description": "Do you agree to the terms and conditions?",
        },
    },
    "required": ["name"],
}


def elicit_result(reply: JsonValue) -> JsonObject:
    """``ElicitResultSchema.parse(reply)``: action first, then content."""
    found = reply if isinstance(reply, dict) else {}
    if found.get("action") not in {"accept", "decline", "cancel"}:
        issue = _choice_issue(["action"], ("accept", "decline", "cancel"))
        raise RpcError(0, jsfmt.stringify([issue]))
    parsed = {
        key: value for key, value in found.items() if not (key == "content" and value is None)
    }
    return _ordered(parsed, ("_meta", "action", "content"))


def _truthy(value: JsonValue) -> bool:
    return value not in (None, "", 0, False) and not (
        isinstance(value, float) and math.isnan(value)
    )


def _user_lines(data: dict[str, Any], fields: list[tuple[str, str, bool]]) -> list[str]:
    lines = []
    for key, label, always in fields:
        value = data.get(key)
        shown = (key in data) if always else _truthy(value)
        if shown:
            lines.append(f"- {label}: {jsfmt.to_string(value)}")
    return lines


_FORM_FIELDS: Final = [
    ("name", "Name", False),
    ("check", "Agreed to terms", True),
    ("color", "Favorite Color", False),
    ("email", "Email", False),
    ("homepage", "Homepage", False),
    ("birthdate", "Birthdate", False),
    ("integer", "Favorite Integer", True),
    ("number", "Favorite Number", True),
    ("petType", "Pet Type", False),
]


async def trigger_elicitation_request(ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """Ask the user to fill a form covering every elicitation field type, and report the answer."""
    params: JsonObject = {
        "message": "Please provide inputs for the following fields:",
        "requestedSchema": ELICITATION_SCHEMA,
    }
    try:
        result = elicit_result(
            await ctx.ask_client("elicitation/create", params, within=ELICITATION_TIMEOUT)
        )
    except ClientError as error:
        return client_failure(error)
    except RpcError as error:
        return failure(error.message)
    content: list[JsonValue] = []
    action, data = result["action"], result.get("content")
    if action == "accept" and isinstance(data, dict):
        content.append(text("✅ User provided the requested information!"))
        lines = _user_lines(data, _FORM_FIELDS)
        content.append(text("User inputs:\n" + "\n".join(lines)))
    elif action == "decline":
        content.append(text("❌ User declined to provide the requested information."))
    elif action == "cancel":
        content.append(text("⚠️ User cancelled the elicitation dialog."))
    content.append(text(f"\nRaw result: {jsfmt.stringify(result)}"))
    return {"content": content}


async def trigger_url_elicitation(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Send the user to a URL, by request (default) or with the -32042 error path."""
    schema = {
        "url": Field("url", required=True),
        "message": Field("string", default="Please open the link to complete this action."),
        "elicitationId": Field("string"),
        "errorPath": Field("boolean", default=False),
    }
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("trigger-url-elicitation", issues)
    url = str(args["url"])  # zod trims it (and drops tabs and newlines)
    requested = args.get("elicitationId")
    elicitation_id = str(requested) if requested is not None else str(uuid.uuid4())
    session = mcp_session(ctx)
    key = f"{session_id(ctx) or 'default'}\u0000{url}\u0000{requested or ''}"
    if args["errorPath"] is True:
        if key in session.url_errors:
            session.url_errors.discard(key)
        else:
            session.url_errors.add(key)
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
    params = {
        "mode": "url",
        "url": url,
        "message": args["message"],
        "elicitationId": elicitation_id,
    }
    try:
        result = elicit_result(
            await ctx.ask_client("elicitation/create", params, within=ELICITATION_TIMEOUT)
        )
    except ClientError as error:
        return client_failure(error)
    except RpcError as error:
        return failure(error.message)
    content: list[JsonValue] = []
    if result["action"] == "accept":
        content.append(
            text(
                "✅ User completed the URL elicitation flow.\n"
                f"Elicitation ID: {elicitation_id}\nURL: {url}"
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
    content.append(text(f"\nRaw result: {jsfmt.stringify(result)}"))
    return {"content": content}


# -- roots -----------------------------------------------------------------------------------------


async def sync_roots(ctx: RequestContext) -> list[Any] | None:
    """``syncRoots``: ask the client for its roots on the GET stream, once, then keep them."""
    session = mcp_session(ctx)
    if session.roots is not None:
        return session.roots
    session.roots_watch = True
    await request_roots(ctx)
    return session.roots


async def request_roots(ctx: RequestContext, *, wait: bool = True) -> None:
    """Ask the client for its roots on its GET stream; log what arrived, as the server does.

    The request rides the client's GET stream. Without one it never arrives (recording 000497:
    a stream opened later never got the request sent at ``initialized``), and the server waits
    out the SDK's 60 s timeout; ``wait=False`` skips that wait where nobody waits on it.
    """
    session = mcp_session(ctx)
    channel = ctx.channel
    if channel is None:  # pragma: no cover - MCP contexts always carry one
        return
    asked = time.monotonic()
    try:
        reply = await channel.ask("roots/list", None, within=SAMPLING_TIMEOUT)
    except ClientError as error:
        unsent = error.code == REQUEST_TIMEOUT and time.monotonic() - asked < UNSENT_SECONDS
        if unsent and wait:
            await ctx.pause(max(SAMPLING_TIMEOUT - (time.monotonic() - asked), 0.0))
        return  # a refusal is only logged on the server's console
    roots = reply.get("roots") if isinstance(reply, dict) else None
    if not isinstance(roots, list):
        return  # fails the SDK's ListRootsResult check: only logged on the server's console
    session.roots = roots
    log(
        ctx,
        "info",
        f"Roots updated: {len(roots)} root(s) received from client",
        logger="everything-server",
    )


async def get_roots_list(ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
    """The roots the client gave (asked once per session)."""
    roots = await sync_roots(ctx)
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
        f"{index}. {(root.get('name') if isinstance(root, dict) else None) or 'Unnamed Root'}\n"
        f"   URI: {jsfmt.to_string(root.get('uri') if isinstance(root, dict) else None)}"
        for index, root in enumerate(roots, start=1)
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


# -- requests the client runs as tasks -------------------------------------------------------------


async def _client_task(
    ctx: RequestContext, task: JsonObject, polls: int, *, sparse: bool
) -> tuple[str, str | None, list[str], bool]:
    """Poll the client's task until it ends: final status, its message, the log, timed out."""
    task_id = str(task.get("taskId"))
    status = str(task.get("status"))
    message: str | None = None
    log_lines = [f"Task created: {task_id}"]
    attempts = 0
    while status not in {"completed", "failed", "cancelled"} and attempts < polls:
        await ctx.pause(POLL_INTERVAL)
        attempts += 1
        polled = await ctx.ask_client("tasks/get", {"taskId": task_id})
        found = polled if isinstance(polled, dict) else {}
        status = str(found.get("status"))
        raw = found.get("statusMessage")
        message = raw if isinstance(raw, str) else None
        if not sparse or attempts == 1 or attempts % 10 == 0 or status != "input_required":
            log_lines.append(f"Poll {attempts}: {status}" + (f" - {message}" if message else ""))
    return status, message, log_lines, attempts >= polls


def _ended(status: str, message: str | None, log_lines: list[str]) -> JsonObject:
    progress = "\n".join(log_lines)
    return {
        "content": [text(f"[{status.upper()}] {message or 'No message'}\n\nProgress:\n{progress}")]
    }


def _timed_out(polls: int, log_lines: list[str]) -> JsonObject:
    progress = "\n".join(log_lines)
    return {
        "content": [
            text(f"[TIMEOUT] Task timed out after {polls} poll attempts\n\nProgress:\n{progress}")
        ]
    }


async def trigger_sampling_request_async(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Ask the client to sample as a task, poll it every second, then fetch its result."""
    schema = {"prompt": Field("string", required=True), "maxTokens": Field("number", default=100)}
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        return invalid("trigger-sampling-request-async", issues)
    params = _sampling_params(args, "trigger-sampling-request-async", {"ttl": 300000})
    try:
        reply = await ctx.ask_client("sampling/createMessage", params, within=SAMPLING_TIMEOUT)
        found = reply if isinstance(reply, dict) else {}
        task = found.get("task")
        if not isinstance(task, dict):
            synced = _ordered(found, ("role", "content", "model", "stopReason"), extras=False)
            return {
                "content": [
                    text(f"[SYNC] Client executed synchronously:\n{jsfmt.stringify(synced)}")
                ]
            }
        status, message, log_lines, timed_out = await _client_task(
            ctx, task, SAMPLING_POLLS, sparse=False
        )
        if timed_out:
            return _timed_out(SAMPLING_POLLS, log_lines)
        if status in {"failed", "cancelled"}:
            return _ended(status, message, log_lines)
        result = await ctx.ask_client("tasks/result", {"taskId": task.get("taskId")})
    except ClientError as error:
        return client_failure(error)
    progress = "\n".join(log_lines)
    return {
        "content": [
            text(
                "[COMPLETED] Async sampling completed!\n\n**Progress:**\n"
                f"{progress}\n\n**Result:**\n{jsfmt.stringify(result)}"
            )
        ]
    }


_ASYNC_FIELDS: Final = [
    ("name", "Name", False),
    ("favoriteColor", "Favorite Color", False),
    ("agreeToTerms", "Agreed to terms", True),
]


async def trigger_elicitation_request_async(
    ctx: RequestContext, _arguments: JsonObject
) -> JsonValue:
    """Ask the client to run an elicitation as a task, poll it, then fetch the user's answer."""
    params: JsonObject = {
        "task": {"ttl": 600000},
        "message": "Please provide inputs for the following fields (async task demo):",
        "requestedSchema": ASYNC_ELICITATION_SCHEMA,
    }
    try:
        reply = await ctx.ask_client("elicitation/create", params, within=SAMPLING_TIMEOUT)
        found = reply if isinstance(reply, dict) else {}
        task = found.get("task")
        if not isinstance(task, dict):
            synced = _ordered(found, ("action", "content"), extras=False)
            return {
                "content": [
                    text(f"[SYNC] Client executed synchronously:\n{jsfmt.stringify(synced)}")
                ]
            }
        status, message, log_lines, timed_out = await _client_task(
            ctx, task, ELICITATION_POLLS, sparse=True
        )
        if timed_out:
            return _timed_out(ELICITATION_POLLS, log_lines)
        if status in {"failed", "cancelled"}:
            return _ended(status, message, log_lines)
        result = await ctx.ask_client("tasks/result", {"taskId": task.get("taskId")})
    except ClientError as error:
        return client_failure(error)
    answer = result if isinstance(result, dict) else {}
    content: list[JsonValue] = []
    data = answer.get("content")
    if answer.get("action") == "accept" and _truthy(data) and isinstance(data, dict):
        content.append(text("[COMPLETED] User provided the requested information!"))
        content.append(text("User inputs:\n" + "\n".join(_user_lines(data, _ASYNC_FIELDS))))
    elif answer.get("action") == "decline":
        content.append(text("[DECLINED] User declined to provide the requested information."))
    elif answer.get("action") == "cancel":
        content.append(text("[CANCELLED] User cancelled the elicitation dialog."))
    progress = "\n".join(log_lines)
    content.append(text(f"\nProgress:\n{progress}\n\nRaw result: {jsfmt.stringify(result)}"))
    return {"content": content}


# -- research tasks (served by tasks/* in methods.py) ----------------------------------------------

_TASK_REFUSAL: Final = "MCP error -32602: Invalid task creation result: " + jsfmt.issue_json(
    ["task"], "object", "undefined"
)


async def simulate_research_query(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    """Start a research task (task-augmented calls only); tasks/* report its progress."""
    request = ctx.json_body if isinstance(ctx.json_body, dict) else {}
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    assert isinstance(params, dict)  # noqa: S101
    if not isinstance(params.get("task"), dict):
        return failure(
            "MCP error -32601: Tool simulate-research-query requires task augmentation "
            "(taskSupport: 'required')"
        )
    schema = {"topic": Field("string", required=True), "ambiguous": Field("boolean", default=False)}
    args, issues = jsfmt.check(schema, arguments)
    if issues:
        raise RpcError(-32602, _TASK_REFUSAL)
    elicits = "elicitation" in ctx.client_capabilities
    task = ResearchTask(
        task_id=uuid.uuid4().hex,
        topic=str(args["topic"]),
        ambiguous=bool(args["ambiguous"]) and elicits,
        created_ns=ctx.now_ns,
    )
    mcp_session(ctx).tasks[task.task_id] = task
    from fauxcall_everything.research import describe  # noqa: PLC0415 - avoids an import cycle

    return {"task": describe(task, ctx.now_ns)}


TOOL_HANDLERS: Final[dict[str, ToolHandler]] = {
    "echo": echo,
    "get-annotated-message": get_annotated_message,
    "get-env": get_env,
    "get-resource-links": get_resource_links,
    "get-resource-reference": get_resource_reference,
    "get-structured-content": get_structured_content,
    "get-sum": get_sum,
    "get-tiny-image": get_tiny_image,
    "gzip-file-as-resource": gzip_file_as_resource,
    "toggle-simulated-logging": toggle_simulated_logging,
    "toggle-subscriber-updates": toggle_subscriber_updates,
    "trigger-long-running-operation": trigger_long_running_operation,
    "simulate-research-query": simulate_research_query,
    "trigger-sampling-request": trigger_sampling_request,
    "trigger-elicitation-request": trigger_elicitation_request,
    "trigger-url-elicitation": trigger_url_elicitation,
    "get-roots-list": get_roots_list,
    "trigger-sampling-request-async": trigger_sampling_request_async,
    "trigger-elicitation-request-async": trigger_elicitation_request_async,
}
