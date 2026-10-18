"""Resources, prompts, completions, subscriptions and logging levels (``METHOD_HANDLERS``).

* ``resources/read``: the static documents as recorded, the two dynamic templates (text and blob,
  stamped with the time of day), and the session's gzip resources. URIs are looked up the way the
  SDK does, after ``new URL(uri)`` normalized them (``my file.gz`` is read as ``my%20file.gz``).
* ``resources/list``: the static documents, then the session's resources in registration order.
* ``prompts/get`` and ``completion/complete``: the four prompts and their completers.
* ``resources/subscribe``/``unsubscribe`` and ``logging/setLevel``: kept per client, with the
  ``info`` log messages the real server sends about subscriptions.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, Final

from fauxcall.runtime import RpcError

from fauxcall_everything import js, requests, zod
from fauxcall_everything.state import (
    LOG_SEVERITY,
    SessionResource,
    log_message,
    server_of,
    session_of,
)

if TYPE_CHECKING:
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

TEXT_TEMPLATE: Final = "demo://resource/dynamic/text/{resourceId}"
BLOB_TEMPLATE: Final = "demo://resource/dynamic/blob/{resourceId}"
_TEMPLATES: Final = (
    (TEXT_TEMPLATE, re.compile(r"^demo://resource/dynamic/text/([^/,]+)$")),
    (BLOB_TEMPLATE, re.compile(r"^demo://resource/dynamic/blob/([^/,]+)$")),
)
RESOURCE_TYPES: Final = ("Text", "Blob")
DEPARTMENTS: Final = ("Engineering", "Sales", "Marketing", "Support")
TEAMS: Final = {
    "Engineering": ("Alice", "Bob", "Charlie"),
    "Sales": ("David", "Eve", "Frank"),
    "Marketing": ("Grace", "Henry", "Iris"),
    "Support": ("John", "Kim", "Lee"),
}
EMPTY_COMPLETION: Final[JsonObject] = {"completion": {"values": [], "hasMore": False}}
MAX_COMPLETIONS: Final = 100


def _request_error(issue: zod.Issue) -> RpcError:
    """A request the SDK's schema refuses: ``-32603`` with zod's issues."""
    return RpcError(-32603, zod.issues_text([issue]))


def _uri(ctx: RequestContext, params: JsonObject) -> str:
    """The required ``uri`` of ``resources/read`` and the subscriptions."""
    requests.refuse([issue] if (issue := requests.params_issue(ctx)) else [])
    uri = params.get("uri", zod.MISSING)
    if not isinstance(uri, str):
        raise _request_error(zod.wrong_type("string", uri, ["params", "uri"]))
    return uri


def _string_record(value: object, path: zod.Path) -> list[zod.Issue]:
    """``z.record(z.string(), z.string())``'s issues."""
    if not isinstance(value, dict):
        return [zod.wrong_type("record", value, path)]
    return [
        zod.wrong_type("string", item, [*path, key])
        for key, item in value.items()
        if not isinstance(item, str)
    ]


# -- dynamic resources -----------------------------------------------------------------------------


def text_resource(ctx: RequestContext, resource_id: float, uri: str | None = None) -> JsonObject:
    """``textResource``: a plaintext resource stamped with the time of day."""
    shown = js.js_number(resource_id)
    return {
        "uri": uri or f"demo://resource/dynamic/text/{shown}",
        "mimeType": "text/plain",
        "text": f"Resource {shown}: This is a plaintext resource created at "
        f"{js.time_of_day(ctx.now_ns)}",
    }


def blob_resource(ctx: RequestContext, resource_id: float, uri: str | None = None) -> JsonObject:
    """``blobResource``: the same sentence as base64 (and, as on the real server, text/plain)."""
    shown = js.js_number(resource_id)
    sentence = f"Resource {shown}: This is a base64 blob created at {js.time_of_day(ctx.now_ns)}"
    return {
        "uri": uri or f"demo://resource/dynamic/blob/{shown}",
        "mimeType": "text/plain",
        "blob": base64.b64encode(sentence.encode()).decode("ascii"),
    }


def _static_uris(ctx: RequestContext) -> list[str]:
    mcp = ctx.model.mcp
    resources = mcp.resources if mcp is not None else []
    return [str(r["uri"]) for r in resources if isinstance(r, dict) and "uri" in r]


def _recorded_read(ctx: RequestContext, uri: str) -> JsonValue:
    """A static document's recorded ``resources/read`` result."""
    for exchange in ctx.runtime.recordings.for_endpoint(f"MCP resources/read:{uri}"):
        reply = exchange.response.body.parsed() if exchange.response else None
        if isinstance(reply, dict) and isinstance(reply.get("result"), dict):
            return reply["result"]
    return None


async def read_resource(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/read``: registered resources first, then the templates, else not found."""
    raw = _uri(ctx, params)
    url = js.parse_url(raw)
    if url is None:
        raise RpcError(-32603, "Invalid URL")
    href = url.href
    session = session_of(ctx)
    if href in session.resources:
        found = session.resources[href]
        return {"contents": [{"uri": href, "mimeType": found.mime_type, "blob": found.blob}]}
    if href in _static_uris(ctx):
        recorded = _recorded_read(ctx, href)
        if recorded is not None:
            return recorded
    for template, pattern in _TEMPLATES:
        match = pattern.match(href)
        if match is None:
            continue
        resource_id = js.js_to_number(match.group(1))
        if not js.is_positive_integer(resource_id):
            raise RpcError(-32603, f"Unknown resource: {href}")
        make = text_resource if template == TEXT_TEMPLATE else blob_resource
        return {"contents": [make(ctx, resource_id, href)]}
    raise RpcError(-32602, f"MCP error -32602: Resource {href} not found")


async def list_resources(ctx: RequestContext, _params: JsonObject) -> JsonValue:
    """``resources/list``: the static documents, then this session's resources."""
    mcp = ctx.model.mcp
    static = list(mcp.resources) if mcp is not None else []
    session = session_of(ctx)
    return {"resources": [*static, *(r.listing() for r in session.resources.values())]}


def register_session_resource(
    ctx: RequestContext, uri: str, name: str, mime_type: str, blob: str
) -> None:
    """``registerSessionResource``: replace any earlier one with this URI, in any session.

    Each removal and each registration tells that client its resource list changed.
    """
    server = server_of(ctx)
    session = session_of(ctx)
    owner = server.resource_owners.get(uri)
    if owner is not None:
        previous = server.sessions.get(owner)
        if previous is not None and previous.resources.pop(uri, None) is not None:
            server.channel(owner).push("notifications/resources/list_changed")
    session.resources[uri] = SessionResource(uri, name, mime_type, blob)
    server.resource_owners[uri] = session.id
    assert ctx.channel is not None  # noqa: S101 - every MCP call has one
    ctx.channel.push("notifications/resources/list_changed")


# -- subscriptions and logging ---------------------------------------------------------------------


async def subscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/subscribe``: remember the subscriber, acknowledge it in a log message."""
    uri = _uri(ctx, params)
    server, session = server_of(ctx), session_of(ctx)
    assert ctx.channel is not None  # noqa: S101 - every MCP call has one
    origin = f"from session {session.id}" if session.id else ""
    log_message(
        ctx.channel, session, "info", f"Received Subscribe Resource request for URI: {uri} {origin}"
    )
    subscribers = server.subscriptions.setdefault(uri, [])
    if session.id not in subscribers:
        subscribers.append(session.id)
    return {}


async def unsubscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``resources/unsubscribe``: forget the subscriber (the URI keeps its place)."""
    uri = _uri(ctx, params)
    server, session = server_of(ctx), session_of(ctx)
    assert ctx.channel is not None  # noqa: S101 - every MCP call has one
    origin = f"from session {session.id}" if session.id else ""
    log_message(
        ctx.channel, session, "info", f"Received Unsubscribe Resource request: {uri} {origin}"
    )
    subscribers = server.subscriptions.get(uri)
    if subscribers is not None and session.id in subscribers:
        subscribers.remove(session.id)
    return {}


async def set_level(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``logging/setLevel``: messages below this level are no longer sent to this client."""
    requests.refuse([issue] if (issue := requests.params_issue(ctx)) else [])
    level = params.get("level", zod.MISSING)
    if not isinstance(level, str) or level not in LOG_SEVERITY:
        raise _request_error(zod.not_one_of(tuple(LOG_SEVERITY), ["params", "level"]))
    session_of(ctx).log_level = level
    return {}


# -- prompts ---------------------------------------------------------------------------------------


PROMPT_ARGS: Final[dict[str, tuple[tuple[str, bool], ...]]] = {
    "simple-prompt": (),
    "args-prompt": (("city", True), ("state", False)),
    "completable-prompt": (("department", True), ("name", True)),
    "resource-prompt": (("resourceType", True), ("resourceId", True)),
}
"""Each prompt's arguments: (name, required). ``simple-prompt`` has no schema at all."""


def _user(content: JsonObject) -> JsonObject:
    return {"role": "user", "content": content}


def _prompt_request(ctx: RequestContext, params: JsonObject) -> tuple[str, dict[str, str]]:
    """``GetPromptRequestSchema``: a name and string arguments (every issue, in order)."""
    requests.refuse([issue] if (issue := requests.params_issue(ctx)) else [])
    name = params.get("name", zod.MISSING)
    issues = [] if isinstance(name, str) else [zod.wrong_type("string", name, ["params", "name"])]
    arguments = params.get("arguments", {})
    issues += _string_record(arguments, ["params", "arguments"])
    requests.refuse(issues)
    assert isinstance(name, str)  # noqa: S101 - checked above
    assert isinstance(arguments, dict)  # noqa: S101 - checked above
    return name, {key: str(value) for key, value in arguments.items()}


async def get_prompt(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``prompts/get``: the request checked, then the prompt's arguments, then its messages."""
    name, given = _prompt_request(ctx, params)
    if name not in PROMPT_ARGS:
        raise RpcError(-32602, f"MCP error -32602: Prompt {name} not found")
    if name == "simple-prompt":
        return {
            "messages": [
                _user({"type": "text", "text": "This is a simple prompt without arguments."})
            ]
        }
    issues = [
        zod.wrong_type("string", zod.MISSING, [arg])
        for arg, required in PROMPT_ARGS[name]
        if required and arg not in given
    ]
    if issues:
        detail = "\n".join(f"{issue.message} at {zod.dot_path(issue.path)}" for issue in issues)
        raise RpcError(-32602, f"MCP error -32602: Invalid arguments for prompt {name}: {detail}")
    if name == "args-prompt":
        state = given.get("state")
        location = given["city"] + (f", {state}" if state else "")
        return {"messages": [_user({"type": "text", "text": f"What's weather in {location}?"})]}
    if name == "completable-prompt":
        message = f"Please promote {given['name']} to the head of the {given['department']} team."
        return {"messages": [_user({"type": "text", "text": message})]}
    return _resource_prompt(ctx, given)


def _resource_prompt(ctx: RequestContext, given: dict[str, str]) -> JsonValue:
    resource_type, raw_id = given["resourceType"], given["resourceId"]
    if resource_type not in RESOURCE_TYPES:
        raise RpcError(-32603, f"Invalid resourceType: {resource_type}. Must be Text or Blob.")
    resource_id = js.js_to_number(raw_id)
    if not js.is_positive_integer(resource_id):
        raise RpcError(-32603, f"Invalid resourceId: {raw_id}. Must be a finite positive integer.")
    make = text_resource if resource_type == "Text" else blob_resource
    intro = (
        f"This prompt includes the {resource_type} resource with id: {js.js_number(resource_id)}. "
        "Please analyze the following resource:"
    )
    return {
        "messages": [
            _user({"type": "text", "text": intro}),
            _user({"type": "resource", "resource": make(ctx, resource_id)}),
        ]
    }


# -- completions -----------------------------------------------------------------------------------


def _completed(values: list[str]) -> JsonObject:
    """``createCompletionResult``: at most 100 values, the total and whether there are more."""
    shown: list[JsonValue] = list(values[:MAX_COMPLETIONS])
    return {
        "completion": {
            "values": shown,
            "total": len(values),
            "hasMore": len(values) > MAX_COMPLETIONS,
        }
    }


def _positive_id(value: str) -> list[str]:
    return [value] if js.is_positive_integer(js.js_to_number(value)) else []


def _reference_issues(ref: object) -> zod.Issue | None:
    """The ``ref`` union: a prompt reference or a resource-template reference."""
    if not isinstance(ref, dict):
        return zod.Issue(
            {
                "code": "invalid_union",
                "errors": [
                    [zod.wrong_type("object", ref, []).as_json()],
                    [zod.wrong_type("object", ref, []).as_json()],
                ],
            },
            ["params", "ref"],
            "Invalid input",
        )
    prompt: list[zod.Issue] = []
    resource: list[zod.Issue] = []
    if ref.get("type") != "ref/prompt":
        prompt.append(zod.not_literal("ref/prompt", ["type"]))
    if not isinstance(ref.get("name"), str):
        prompt.append(zod.wrong_type("string", ref.get("name", zod.MISSING), ["name"]))
    if ref.get("type") != "ref/resource":
        resource.append(zod.not_literal("ref/resource", ["type"]))
    if not isinstance(ref.get("uri"), str):
        resource.append(zod.wrong_type("string", ref.get("uri", zod.MISSING), ["uri"]))
    if not prompt or not resource:
        return None
    return zod.Issue(
        {
            "code": "invalid_union",
            "errors": [
                [issue.as_json() for issue in prompt],
                [issue.as_json() for issue in resource],
            ],
        },
        ["params", "ref"],
        "Invalid input",
    )


def _argument_issues(argument: object) -> list[zod.Issue]:
    if not isinstance(argument, dict):
        return [zod.wrong_type("object", argument, ["params", "argument"])]
    return [
        zod.wrong_type("string", argument.get(key, zod.MISSING), ["params", "argument", key])
        for key in ("name", "value")
        if not isinstance(argument.get(key), str)
    ]


def _context_issues(params: JsonObject) -> list[zod.Issue]:
    """``context``: optional, with optional string ``arguments`` (recording 000488)."""
    if "context" not in params:
        return []
    context = params["context"]
    if not isinstance(context, dict):
        return [zod.wrong_type("object", context, ["params", "context"])]
    if "arguments" not in context:
        return []
    return _string_record(context["arguments"], ["params", "context", "arguments"])


async def complete(ctx: RequestContext, params: JsonObject) -> JsonValue:  # noqa: PLR0911
    """``completion/complete``: the prompt's or the resource template's completer."""
    requests.refuse([issue] if (issue := requests.params_issue(ctx)) else [])
    ref: object = params.get("ref", zod.MISSING)
    union = _reference_issues(ref)
    issues = [union] if union is not None else []
    issues += _argument_issues(params.get("argument", zod.MISSING))
    issues += _context_issues(params)
    requests.refuse(issues)
    assert isinstance(ref, dict)  # noqa: S101 - checked above
    argument = params["argument"]
    assert isinstance(argument, dict)  # noqa: S101 - checked above
    name, value = str(argument["name"]), str(argument["value"])
    context = params.get("context")
    known = context.get("arguments") if isinstance(context, dict) else None
    if ref.get("type") == "ref/prompt":
        prompt = str(ref["name"])
        if prompt not in PROMPT_ARGS:
            raise RpcError(-32602, f"MCP error -32602: Prompt {prompt} not found")
        if prompt == "completable-prompt" and name == "department":
            return _completed([d for d in DEPARTMENTS if d.startswith(value)])
        if prompt == "completable-prompt" and name == "name":
            department = known.get("department") if isinstance(known, dict) else None
            team = TEAMS.get(department, ()) if isinstance(department, str) else ()
            return _completed([member for member in team if member.startswith(value)])
        if prompt == "resource-prompt" and name == "resourceType":
            return _completed([t for t in RESOURCE_TYPES if t.startswith(value)])
        if prompt == "resource-prompt" and name == "resourceId":
            return _completed(_positive_id(value))
        return EMPTY_COMPLETION
    uri = str(ref["uri"])
    if uri in {TEXT_TEMPLATE, BLOB_TEMPLATE}:
        return _completed(_positive_id(value)) if name == "resourceId" else EMPTY_COMPLETION
    if uri in _static_uris(ctx) or uri in session_of(ctx).resources:
        return EMPTY_COMPLETION
    raise RpcError(-32602, f"MCP error -32602: Resource template {uri} not found")
