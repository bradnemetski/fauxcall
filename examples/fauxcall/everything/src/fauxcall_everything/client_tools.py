"""Tools that ask the client something mid-call: sampling, elicitation, roots, and task variants.

The task-based variants then poll the client's own task.

The client's answers are parsed as the SDK parses them (zod: known members first, in schema
order; unknown ones dropped or kept as the schema says), so the texts quoting them match.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Final

from fauxcall.model.mcp import has_capability
from fauxcall.runtime import ClientError, RpcError

from fauxcall_everything import jsfmt, zod
from fauxcall_everything.common import (
    ClientState,
    ToolError,
    client_state,
    log_message,
    mcp_error_text,
    text,
    validated_tool,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.types import JsonObject, JsonValue

ELICITATION_TIMEOUT: Final = 600.0
"""``trigger-elicitation-request`` and ``trigger-url-elicitation`` wait 10 minutes."""
CLIENT_TIMEOUT: Final = 60.0
"""Every other request to the client: the SDK's default timeout."""
URL_ELICITATION_REQUIRED: Final = -32042
REQUEST_TIMEOUT: Final = -32001
SAMPLING_POLLS: Final = 60
ELICITATION_POLLS: Final = 600
ROOTS_SYNC_DELAY: Final = 0.35
"""The server asks a roots-capable client for its roots 350 ms after it is initialized."""
SAMPLING_FIELDS: Final = [
    zod.Field("prompt", "string"),
    zod.Field("maxTokens", "number", default=100),
]
_CONTENT_MEMBERS: Final = {
    "text": ("type", "text", "annotations", "_meta"),
    "image": ("type", "data", "mimeType", "annotations", "_meta"),
    "audio": ("type", "data", "mimeType", "annotations", "_meta"),
}


class ResultError(Exception):
    """The client's answer doesn't match the schema: the tool fails with zod's issues."""

    def __init__(self, issues: list[zod.Issue]) -> None:
        super().__init__(zod.issues_json(issues))


def _ordered(value: JsonObject, members: tuple[str, ...], *, keep_unknown: bool) -> JsonObject:
    """``value`` with ``members`` first in that order (absent ones skipped), then the rest."""
    out: JsonObject = {
        key: value[key] for key in members if key in value and value[key] is not None
    }
    if keep_unknown:
        out.update({key: item for key, item in value.items() if key not in members})
    return out


def _content_issues(content: JsonValue) -> list[zod.Issue]:
    """``SamplingContentSchema``: a text, image or audio block (a union on ``type``)."""
    if not isinstance(content, dict):
        return [zod.invalid_type("object", content, ["content"])]
    kind = content.get("type")
    if kind not in _CONTENT_MEMBERS:
        options = list(_CONTENT_MEMBERS)
        expected = " | ".join(f"'{option}'" for option in options)
        members: dict[str, JsonValue] = {
            "code": "invalid_union",
            "errors": [],
            "note": "No matching discriminator",
            "discriminator": "type",
            "options": list(options),
            "message": f"Invalid discriminator value. Expected {expected}",
        }
        return [zod.Issue(members, ["content", "type"])]
    required = ("text",) if kind == "text" else ("data", "mimeType")
    return [
        zod.invalid_type("string", content.get(name, jsfmt.UNDEFINED), ["content", name])
        for name in required
        if not isinstance(content.get(name), str)
    ]


def parse_sampling_result(result: JsonValue) -> JsonObject:
    """``CreateMessageResultSchema``: ``_meta, model, stopReason, role, content`` + the rest."""
    if not isinstance(result, dict):
        raise ResultError([zod.invalid_type("object", result, [])])
    issues: list[zod.Issue] = []
    if not isinstance(result.get("model"), str):
        issues.append(zod.invalid_type("string", result.get("model", jsfmt.UNDEFINED), ["model"]))
    if result.get("role") not in {"user", "assistant"}:
        issues.append(zod.invalid_value(("user", "assistant"), ["role"]))
    content = result.get("content", jsfmt.UNDEFINED)
    issues += _content_issues(content)  # type: ignore[arg-type]
    if issues:
        raise ResultError(issues)
    assert isinstance(content, dict)  # noqa: S101 - checked above
    parsed = _ordered(
        result, ("_meta", "model", "stopReason", "role", "content"), keep_unknown=True
    )
    parsed["content"] = _ordered(
        content, _CONTENT_MEMBERS[str(content["type"])], keep_unknown=False
    )
    return parsed


def _form_value_issue(key: str, value: JsonValue) -> zod.Issue | None:
    """A form value that is none of string, number, boolean, list of strings (zod's union)."""
    if isinstance(value, str | int | float) and not (isinstance(value, float) and value != value):  # noqa: PLR0124
        return None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return None
    if isinstance(value, list):
        array_errors: list[JsonValue] = [
            zod.invalid_type("string", item, [index]).as_json()
            for index, item in enumerate(value)
            if not isinstance(item, str)
        ]
    else:
        array_errors = [zod.invalid_type("array", value, []).as_json()]
    errors: list[JsonValue] = [
        [zod.invalid_type(kind, value, []).as_json()] for kind in ("string", "number", "boolean")
    ]
    errors.append(array_errors)
    members: dict[str, JsonValue] = {
        "code": "invalid_union",
        "errors": errors,
        "message": "Invalid input",
    }
    return zod.Issue(members, ["content", key])


def parse_elicit_result(result: JsonValue) -> JsonObject:
    """``ElicitResultSchema``: ``_meta, action, content`` (``null`` content dropped) + the rest."""
    if not isinstance(result, dict):
        raise ResultError([zod.invalid_type("object", result, [])])
    issues: list[zod.Issue] = []
    if result.get("action") not in {"accept", "decline", "cancel"}:
        issues.append(zod.invalid_value(("accept", "decline", "cancel"), ["action"]))
    content = result.get("content")
    if content is not None and not isinstance(content, dict):
        issues.append(zod.invalid_type("record", content, ["content"]))
    elif isinstance(content, dict):
        found = (_form_value_issue(key, value) for key, value in content.items())
        issues += [issue for issue in found if issue is not None]
    if issues:
        raise ResultError(issues)
    return _ordered(result, ("_meta", "action", "content"), keep_unknown=True)


async def _ask(ctx: RequestContext, method: str, params: JsonObject, within: float) -> JsonValue:
    """Ask the client on this call's stream; a refusal fails the tool as the SDK's McpError."""
    try:
        return await ctx.ask_client(method, params, within=within)
    except ClientError as error:
        raise ToolError(mcp_error_text(error.code, error.message)) from error


def _sampling_request(name: str, args: JsonObject, *, task: bool) -> JsonObject:
    params: JsonObject = {"task": {"ttl": 300000}} if task else {}
    params["messages"] = [
        {
            "role": "user",
            "content": {"type": "text", "text": f"Resource {name} context: {args['prompt']}"},
        }
    ]
    params["systemPrompt"] = "You are a helpful test server."
    params["maxTokens"] = jsfmt.json_number(jsfmt.to_float(args["maxTokens"]))
    params["temperature"] = 0.7
    return params


@validated_tool("trigger-sampling-request", SAMPLING_FIELDS)
async def trigger_sampling_request(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """Ask the client's LLM; answer with what it said, as the SDK parsed it."""
    answer = await _ask(
        ctx,
        "sampling/createMessage",
        _sampling_request("trigger-sampling-request", args, task=False),
        CLIENT_TIMEOUT,
    )
    try:
        parsed = parse_sampling_result(answer)
    except ResultError as error:
        raise ToolError(str(error)) from error
    return {"content": [text(f"LLM sampling result: \n{jsfmt.stringify(parsed)}")]}


FORM_SCHEMA: Final[JsonObject] = {
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
            "description": (
                "Your email address (will be verified, and never shared with anyone else)"
            ),
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
            "description": (
                "Your favorite integer (do not give us your phone number, pin, or other "
                "sensitive info)"
            ),
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
_FORM_LINES: Final = (
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
"""(field, label, shown only when truthy — else whenever present)."""


def truthy(value: JsonValue | None) -> bool:
    """JavaScript truthiness of a JSON value."""
    if value is None or value is False or value == "":
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = jsfmt.to_float(value)
        return number == number and number != 0  # NaN and 0 are falsy  # noqa: PLR0124
    return True


def _input_lines(content: JsonObject, lines: tuple[tuple[str, str, bool], ...]) -> list[str]:
    shown: list[str] = []
    for key, label, needs_truthy in lines:
        value = content.get(key)
        if (needs_truthy and truthy(value)) or (not needs_truthy and key in content):
            shown.append(f"- {label}: {jsfmt.js_string(value)}")
    return shown


@validated_tool("trigger-elicitation-request", [])
async def trigger_elicitation_request(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """Ask the user for a form; answer with what they did and the raw answer."""
    del args
    request: JsonObject = {
        "message": "Please provide inputs for the following fields:",
        "requestedSchema": FORM_SCHEMA,
    }
    answer = await _ask(ctx, "elicitation/create", request, ELICITATION_TIMEOUT)
    try:
        result = parse_elicit_result(answer)
    except ResultError as error:
        raise ToolError(str(error)) from error
    content: list[JsonValue] = []
    form = result.get("content")
    if result["action"] == "accept" and isinstance(form, dict):
        content.append(text("✅ User provided the requested information!"))
        content.append(text("User inputs:\n" + "\n".join(_input_lines(form, _FORM_LINES))))
    elif result["action"] == "decline":
        content.append(text("❌ User declined to provide the requested information."))
    elif result["action"] == "cancel":
        content.append(text("⚠️ User cancelled the elicitation dialog."))
    content.append(text(f"\nRaw result: {jsfmt.stringify(result)}"))
    return {"content": content}


@validated_tool(
    "trigger-url-elicitation",
    [
        zod.Field("url", "url"),
        zod.Field("message", "string", default="Please open the link to complete this action."),
        zod.Field("elicitationId", "string", optional=True),
        zod.Field("errorPath", "boolean", default=False),
    ],
)
async def trigger_url_elicitation(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """Send the user to a URL: by asking (default), or first by the -32042 error path."""
    url, requested = str(args["url"]), args.get("elicitationId")
    elicitation_id = str(requested) if requested is not None else str(uuid.uuid4())
    state = client_state(ctx)
    key = "\x00".join((state.session or "default", url, str(requested or "")))
    if args["errorPath"]:
        if key in state.issued_url_elicitations:
            state.issued_url_elicitations.discard(key)  # the retry: carry on as asked
        else:
            state.issued_url_elicitations.add(key)
            prerequisite: JsonObject = {
                "mode": "url",
                "url": "https://modelcontextprotocol.io",
                "message": "Open this link to satisfy the prerequisite, then retry the request.",
                "elicitationId": str(uuid.uuid4()),
            }
            raise RpcError(
                URL_ELICITATION_REQUIRED,
                mcp_error_text(
                    URL_ELICITATION_REQUIRED, "This request requires browser-based authorization."
                ),
                {"elicitations": [prerequisite]},
            )
    request: JsonObject = {
        "mode": "url",
        "url": url,
        "message": args["message"],
        "elicitationId": elicitation_id,
    }
    answer = await _ask(ctx, "elicitation/create", request, ELICITATION_TIMEOUT)
    try:
        result = parse_elicit_result(answer)
    except ResultError as error:
        raise ToolError(str(error)) from error
    action = result["action"]
    if action == "accept":
        done = (
            "✅ User completed the URL elicitation flow.\n"
            f"Elicitation ID: {elicitation_id}\nURL: {url}"
        )
    elif action == "decline":
        done = f"❌ User declined to open the URL (Elicitation ID: {elicitation_id})."
    else:
        done = f"⚠️ User cancelled the URL elicitation (Elicitation ID: {elicitation_id})."
    return {"content": [text(done), text(f"\nRaw result: {jsfmt.stringify(result)}")]}


# -- roots -----------------------------------------------------------------------------------


async def _request_roots(
    channel: Channel,
    state: ClientState,
    pause: Callable[[float], Awaitable[None]],
) -> None:
    """``requestRoots``: ask on the client's GET stream; keep and announce what it says.

    With no GET stream open the request reaches nobody, and the server waits out the SDK's
    60 s timeout before it gives up (``pause`` takes that time).
    """
    started = time.monotonic()
    try:
        answer = await channel.ask("roots/list", None, within=CLIENT_TIMEOUT)
    except ClientError as error:
        waited = time.monotonic() - started
        if error.code == REQUEST_TIMEOUT and waited < CLIENT_TIMEOUT:
            await pause(CLIENT_TIMEOUT - waited)
        return
    roots = answer.get("roots") if isinstance(answer, dict) else None
    if not isinstance(roots, list) or not all(
        isinstance(root, dict) and str(root.get("uri", "")).startswith("file://") for root in roots
    ):
        return  # the SDK refuses the answer; the server only logs it to its console
    state.roots = [root for root in roots if isinstance(root, dict)]
    log_message(
        channel.push,
        state,
        "info",
        f"Roots updated: {len(roots)} root(s) received from client",
        logger="everything-server",
    )


def registered_tools(capabilities: JsonObject) -> list[str]:
    """The tools registered when this client is initialized (``registerConditionalTools``)."""

    def declared(path: str) -> bool:
        return has_capability(capabilities, path)

    candidates = [
        ("get-roots-list", declared("roots")),
        ("trigger-elicitation-request", declared("elicitation")),
        ("trigger-url-elicitation", declared("elicitation.url")),
        ("trigger-sampling-request", declared("sampling")),
        ("simulate-research-query", True),
        (
            "trigger-sampling-request-async",
            declared("sampling") and declared("tasks.requests.sampling.createMessage"),
        ),
        (
            "trigger-elicitation-request-async",
            declared("elicitation") and declared("tasks.requests.elicitation.create"),
        ),
    ]
    return [name for name, registered in candidates if registered]


async def initialized(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``notifications/initialized``: the capability tools are registered, roots asked later.

    Each registration tells the client ``notifications/tools/list_changed``; a roots-capable
    client is asked for its roots 350 ms later. A second ``initialized`` changes nothing (the
    server's registrations throw before anything is sent).
    """
    del params
    channel = ctx.channel
    state = client_state(ctx)
    if channel is None or state.initialized:
        return None
    state.initialized = True
    for _ in registered_tools(ctx.client_capabilities):
        channel.push("notifications/tools/list_changed")
    if "roots" not in ctx.client_capabilities:
        return None
    await channel.pause(ROOTS_SYNC_DELAY)
    if state.roots is None:
        state.roots_listening = True
        await _request_roots(channel, state, channel.pause)
    return None


async def roots_changed(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``notifications/roots/list_changed``: ask again (once the server listens for it)."""
    del params
    state = client_state(ctx)
    if state.roots_listening and ctx.channel is not None:
        await _request_roots(ctx.channel, state, ctx.channel.pause)
    return None


@validated_tool("get-roots-list", [])
async def get_roots_list(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """The client's roots: asked once (on its GET stream), then remembered for the session."""
    del args
    state = client_state(ctx)
    if state.roots is None and ctx.channel is not None:
        state.roots_listening = True
        await _request_roots(ctx.channel, state, ctx.pause)
    roots = state.roots
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
    listing = "\n\n".join(
        f"{index + 1}. {root.get('name') or 'Unnamed Root'}\n   URI: {root.get('uri')}"
        for index, root in enumerate(roots)
    )
    return {
        "content": [
            text(
                f"Current MCP Roots ({len(roots)} total):\n\n{listing}\n\n"
                "Note: This server demonstrates the roots protocol capability but doesn't actually "
                "access files. The roots are provided by the MCP client and can be used by servers "
                "that need file system access."
            )
        ]
    }


# -- task-based variants -------------------------------------------------------------------------


Shape = tuple[tuple[str, str], ...]
"""An object schema: (member, kind) in order; kind ``string``, ``number``, ``any`` or ``task``,
with ``?`` when the member may be absent."""
TASK_SHAPE: Final[Shape] = (
    ("taskId", "string"),
    ("status", "string"),
    ("pollInterval", "number?"),
    ("statusMessage", "string?"),
)
CREATED_TASK: Final[Shape] = (("task", "task"),)
SAMPLED: Final[Shape] = (
    ("role", "string"),
    ("content", "any"),
    ("model", "string"),
    ("stopReason", "string?"),
)
ELICITED: Final[Shape] = (("action", "string"), ("content", "any?"))
POLLED: Final[Shape] = (("status", "string"), ("statusMessage", "string?"))


def _object(value: JsonValue, shape: Shape, path: list[str | int]) -> list[zod.Issue]:
    """The issues zod finds in ``value`` against an object schema (``[]``: it parses)."""
    if not isinstance(value, dict):
        return [zod.invalid_type("object", value, path)]
    issues: list[zod.Issue] = []
    for key, kind in shape:
        base, optional = kind.rstrip("?"), kind.endswith("?")
        here = [*path, key]
        if key not in value:
            if optional:
                continue
            if base == "any":
                message = "Invalid input: expected nonoptional, received undefined"
                members: dict[str, JsonValue] = {
                    "code": "invalid_type",
                    "expected": "nonoptional",
                    "message": message,
                }
                issues.append(zod.Issue(members, here))
            else:
                expected = "object" if base == "task" else base
                issues.append(zod.invalid_type(expected, jsfmt.UNDEFINED, here))
            continue
        item = value[key]
        if base == "task":
            issues += _object(item, TASK_SHAPE, here)
        elif base == "string" and not isinstance(item, str):
            issues.append(zod.invalid_type("string", item, here))
        elif base == "number" and (isinstance(item, bool) or not isinstance(item, int | float)):
            issues.append(zod.invalid_type("number", item, here))
    return issues


def _parsed(value: JsonObject, shape: Shape) -> JsonObject:
    """What zod keeps of an object that parses: the schema's members, in its order."""
    kept: JsonObject = {key: value[key] for key, _ in shape if key in value}
    if isinstance(kept.get("task"), dict):
        kept["task"] = _parsed(kept["task"], TASK_SHAPE)  # type: ignore[arg-type]
    return kept


def _task_or_answer(answer: JsonValue, alternative: Shape) -> tuple[JsonObject | None, JsonObject]:
    """The client's ``CreateTaskResult`` or direct answer (a zod union), or the tool fails."""
    created = _object(answer, CREATED_TASK, [])
    if not created:
        assert isinstance(answer, dict)  # noqa: S101 - it parsed
        return _parsed(answer, CREATED_TASK)["task"], {}  # type: ignore[return-value]
    direct = _object(answer, alternative, [])
    if not direct:
        assert isinstance(answer, dict)  # noqa: S101 - it parsed
        return None, _parsed(answer, alternative)
    errors: list[JsonValue] = [
        [issue.as_json() for issue in created],
        [issue.as_json() for issue in direct],
    ]
    union: dict[str, JsonValue] = {
        "code": "invalid_union",
        "errors": errors,
        "message": "Invalid input",
    }
    raise ToolError(zod.issues_json([zod.Issue(union)]))


async def _poll_client_task(
    ctx: RequestContext, task: JsonObject, max_polls: int, *, quiet_input: bool
) -> tuple[str, str | None, list[str], int]:
    """Poll the client's task every second: (status, last message, progress lines, polls)."""
    task_id = str(task["taskId"])
    lines = [f"Task created: {task_id}"]
    status, message, polls = str(task["status"]), None, 0
    while status not in {"completed", "failed", "cancelled"} and polls < max_polls:
        await ctx.pause(1.0)
        polls += 1
        polled = await _ask(ctx, "tasks/get", {"taskId": task_id}, CLIENT_TIMEOUT)
        refused = _object(polled, POLLED, [])
        if refused:
            raise ToolError(zod.issues_json(refused))
        assert isinstance(polled, dict)  # noqa: S101 - it parsed
        status = str(polled["status"])
        raw_message = polled.get("statusMessage")
        message = raw_message if isinstance(raw_message, str) else None
        if not quiet_input or polls == 1 or polls % 10 == 0 or status != "input_required":
            lines.append(f"Poll {polls}: {status}{f' - {message}' if message else ''}")
    return status, message, lines, polls


@validated_tool("trigger-sampling-request-async", SAMPLING_FIELDS)
async def trigger_sampling_request_async(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """Ask for sampling as a client-side task, poll it, then fetch its result."""
    request = _sampling_request("trigger-sampling-request-async", args, task=True)
    answer = await _ask(ctx, "sampling/createMessage", request, CLIENT_TIMEOUT)
    task, sync = _task_or_answer(answer, SAMPLED)
    if task is None:
        return {
            "content": [text(f"[SYNC] Client executed synchronously:\n{jsfmt.stringify(sync)}")]
        }
    status, message, lines, polls = await _poll_client_task(
        ctx, task, SAMPLING_POLLS, quiet_input=False
    )
    progress = "\n".join(lines)
    if polls >= SAMPLING_POLLS:
        return {
            "content": [
                text(f"[TIMEOUT] Task timed out after 60 poll attempts\n\nProgress:\n{progress}")
            ]
        }
    if status in {"failed", "cancelled"}:
        return {
            "content": [
                text(f"[{status.upper()}] {message or 'No message'}\n\nProgress:\n{progress}")
            ]
        }
    result = await _ask(ctx, "tasks/result", {"taskId": task["taskId"]}, CLIENT_TIMEOUT)
    return {
        "content": [
            text(
                "[COMPLETED] Async sampling completed!\n\n**Progress:**\n"
                f"{progress}\n\n**Result:**\n{jsfmt.stringify(result)}"
            )
        ]
    }


ASYNC_FORM: Final[JsonObject] = {
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
_ASYNC_LINES: Final = (
    ("name", "Name", True),
    ("favoriteColor", "Favorite Color", True),
    ("agreeToTerms", "Agreed to terms", False),
)


@validated_tool("trigger-elicitation-request-async", [])
async def trigger_elicitation_request_async(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """Ask for a form as a client-side task, poll it (up to 10 minutes), then fetch the answer."""
    del args
    request: JsonObject = {
        "task": {"ttl": 600000},
        "message": "Please provide inputs for the following fields (async task demo):",
        "requestedSchema": ASYNC_FORM,
    }
    answer = await _ask(ctx, "elicitation/create", request, CLIENT_TIMEOUT)
    task, sync = _task_or_answer(answer, ELICITED)
    if task is None:
        return {
            "content": [text(f"[SYNC] Client executed synchronously:\n{jsfmt.stringify(sync)}")]
        }
    status, message, lines, polls = await _poll_client_task(
        ctx, task, ELICITATION_POLLS, quiet_input=True
    )
    progress = "\n".join(lines)
    if polls >= ELICITATION_POLLS:
        return {
            "content": [
                text(f"[TIMEOUT] Task timed out after 600 poll attempts\n\nProgress:\n{progress}")
            ]
        }
    if status in {"failed", "cancelled"}:
        return {
            "content": [
                text(f"[{status.upper()}] {message or 'No message'}\n\nProgress:\n{progress}")
            ]
        }
    result = await _ask(ctx, "tasks/result", {"taskId": task["taskId"]}, CLIENT_TIMEOUT)
    content: list[JsonValue] = []
    action = result.get("action") if isinstance(result, dict) else None
    form = result.get("content") if isinstance(result, dict) else None
    if action == "accept" and truthy(form) and isinstance(form, dict):
        content.append(text("[COMPLETED] User provided the requested information!"))
        content.append(text("User inputs:\n" + "\n".join(_input_lines(form, _ASYNC_LINES))))
    elif action == "decline":
        content.append(text("[DECLINED] User declined to provide the requested information."))
    elif action == "cancel":
        content.append(text("[CANCELLED] User cancelled the elicitation dialog."))
    content.append(text(f"\nProgress:\n{progress}\n\nRaw result: {jsfmt.stringify(result)}"))
    return {"content": content}
