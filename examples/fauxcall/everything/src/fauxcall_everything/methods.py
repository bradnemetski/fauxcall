"""The Everything server's other MCP methods: resources, prompts, completions, logging, roots."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from fauxcall.runtime import RpcError

from fauxcall_everything import jsfmt, research
from fauxcall_everything.state import (
    LOG_LEVELS,
    assets,
    mcp_session,
    session_id,
    subscriptions,
)
from fauxcall_everything.tools import (
    BLOB_BASE,
    TEXT_BASE,
    dynamic_contents,
    log,
    request_roots,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

    MethodHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]

_TEMPLATE: Final = re.compile(r"^demo://resource/dynamic/(text|blob)/([^/,]+)$")
ROOTS_DELAY: Final = 0.35
"""The server asks for roots 350 ms after the client's ``initialized`` notification."""


def _require_string(params: JsonObject, name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str):
        got = jsfmt.received(value, present=name in params)
        raise RpcError(-32603, jsfmt.issue_json(["params", name], "string", got))
    return value


# -- resources -------------------------------------------------------------------------------------


async def resources_list(ctx: RequestContext, _params: JsonObject) -> JsonValue:
    """The static documents, then this session's gzipped files (templates are never listed)."""
    listed: list[JsonValue] = list(ctx.model.mcp.resources) if ctx.model.mcp else []
    listed.extend(
        {"uri": resource.uri, "name": resource.name, "mimeType": resource.mime_type}
        for resource in mcp_session(ctx).resources.values()
    )
    return {"resources": listed}


async def resources_read(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """A static document, a session file, or a dynamic resource fabricated from its id."""
    raw = _require_string(params, "uri")
    if not jsfmt.is_url(raw):
        raise RpcError(-32603, "Invalid URL")
    uri = jsfmt.href(raw)
    docs = assets()["docs"]
    assert isinstance(docs, dict)  # noqa: S101
    if uri in docs:
        return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": docs[uri]}]}
    stored = mcp_session(ctx).resources.get(uri)
    if stored is not None:
        return {"contents": [{"uri": uri, "mimeType": stored.mime_type, "blob": stored.blob}]}
    match = _TEMPLATE.match(uri)
    if match is None:
        raise RpcError(-32602, f"MCP error -32602: Resource {uri} not found")
    resource_id = jsfmt.to_number(match.group(2))
    if not jsfmt.is_positive_integer(resource_id):
        raise RpcError(-32603, f"Unknown resource: {uri}")
    kind = "Text" if match.group(1) == "text" else "Blob"
    return {"contents": [dynamic_contents(ctx, uri, kind, resource_id)]}


async def resources_subscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Subscribe; the server acknowledges with an info log message."""
    uri = _require_string(params, "uri")
    sid = session_id(ctx)
    log(ctx, "info", f"Received Subscribe Resource request for URI: {uri} {_from(sid)}")
    subscriptions(ctx).setdefault(uri, set()).add(sid)
    return {}


async def resources_unsubscribe(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Unsubscribe; the server acknowledges with an info log message."""
    uri = _require_string(params, "uri")
    sid = session_id(ctx)
    log(ctx, "info", f"Received Unsubscribe Resource request: {uri} {_from(sid)}")
    subscriptions(ctx).get(uri, set()).discard(sid)
    return {}


def _from(sid: str) -> str:
    return f"from session {sid}" if sid else ""


# -- logging ---------------------------------------------------------------------------------------


async def logging_set_level(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Remember the client's level: the server's log messages below it are not sent."""
    level = params.get("level")
    if level not in LOG_LEVELS:
        quoted = "|".join(f'"{name}"' for name in LOG_LEVELS)
        issue: JsonObject = {
            "code": "invalid_value",
            "values": list(LOG_LEVELS),
            "path": ["params", "level"],
            "message": f"Invalid option: expected one of {quoted}",
        }
        raise RpcError(-32603, jsfmt.stringify([issue]))
    mcp_session(ctx).log_level = str(level)
    return {}


# -- prompts ---------------------------------------------------------------------------------------

_PROMPT_ARGS: Final[dict[str, tuple[tuple[str, bool], ...]]] = {
    "simple-prompt": (),
    "args-prompt": (("city", True), ("state", False)),
    "completable-prompt": (("department", True), ("name", True)),
    "resource-prompt": (("resourceType", True), ("resourceId", True)),
}


def _user(content: JsonObject) -> JsonObject:
    return {"role": "user", "content": content}


async def prompts_get(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """One of the four prompts, with the SDK's argument checks."""
    name = _require_string(params, "name")
    if name not in _PROMPT_ARGS:
        raise RpcError(-32602, f"MCP error -32602: Prompt {name} not found")
    raw = params.get("arguments")
    arguments: dict[str, JsonValue] = raw if isinstance(raw, dict) else {}
    for key, value in arguments.items():
        if not isinstance(value, str):
            got = jsfmt.received(value)
            raise RpcError(-32603, jsfmt.issue_json(["params", "arguments", key], "string", got))
    missing = [
        f"Invalid input: expected string, received undefined at {arg}"
        for arg, required in _PROMPT_ARGS[name]
        if required and arg not in arguments
    ]
    if missing:
        detail = "\n".join(missing)
        raise RpcError(-32602, f"MCP error -32602: Invalid arguments for prompt {name}: {detail}")
    if name == "simple-prompt":
        return {
            "messages": [
                _user({"type": "text", "text": "This is a simple prompt without arguments."})
            ]
        }
    if name == "args-prompt":
        state = arguments.get("state")
        location = f"{arguments['city']}{f', {state}' if state else ''}"
        return {"messages": [_user({"type": "text", "text": f"What's weather in {location}?"})]}
    if name == "completable-prompt":
        message = (
            f"Please promote {arguments['name']} to the head of the {arguments['department']} team."
        )
        return {"messages": [_user({"type": "text", "text": message})]}
    return _resource_prompt(ctx, arguments)


def _resource_prompt(ctx: RequestContext, arguments: dict[str, JsonValue]) -> JsonValue:
    kind = str(arguments["resourceType"])
    if kind not in {"Text", "Blob"}:
        raise RpcError(-32603, f"Invalid resourceType: {kind}. Must be Text or Blob.")
    raw_id = str(arguments["resourceId"])
    resource_id = jsfmt.to_number(raw_id)
    if not jsfmt.is_positive_integer(resource_id):
        raise RpcError(-32603, f"Invalid resourceId: {raw_id}. Must be a finite positive integer.")
    shown = jsfmt.number(resource_id)
    uri = f"{TEXT_BASE if kind == 'Text' else BLOB_BASE}/{shown}"
    intro = (
        f"This prompt includes the {kind} resource with id: {shown}. "
        "Please analyze the following resource:"
    )
    resource = dynamic_contents(ctx, uri, kind, resource_id)
    return {
        "messages": [
            _user({"type": "text", "text": intro}),
            _user({"type": "resource", "resource": resource}),
        ]
    }


# -- completions -----------------------------------------------------------------------------------

_DEPARTMENTS: Final = ("Engineering", "Sales", "Marketing", "Support")
_TEAMS: Final[dict[str, tuple[str, ...]]] = {
    "Engineering": ("Alice", "Bob", "Charlie"),
    "Sales": ("David", "Eve", "Frank"),
    "Marketing": ("Grace", "Henry", "Iris"),
    "Support": ("John", "Kim", "Lee"),
}
_TEMPLATES: Final = (f"{TEXT_BASE}/{{resourceId}}", f"{BLOB_BASE}/{{resourceId}}")
_BAD_REF: Final = (
    '[\n  {\n    "code": "invalid_union",\n    "errors": [\n      [\n        {\n'
    '          "code": "invalid_value",\n          "values": [\n            "ref/prompt"\n'
    '          ],\n          "path": [\n            "type"\n          ],\n'
    '          "message": "Invalid input: expected \\"ref/prompt\\""\n        }\n      ],\n'
    '      [\n        {\n          "code": "invalid_value",\n          "values": [\n'
    '            "ref/resource"\n          ],\n          "path": [\n            "type"\n'
    '          ],\n          "message": "Invalid input: expected \\"ref/resource\\""\n'
    '        },\n        {\n          "expected": "string",\n          "code": "invalid_type",\n'
    '          "path": [\n            "uri"\n          ],\n'
    '          "message": "Invalid input: expected string, received undefined"\n        }\n'
    '      ]\n    ],\n    "path": [\n      "params",\n      "ref"\n    ],\n'
    '    "message": "Invalid input"\n  }\n]'
)


def _resource_id_values(value: str) -> list[str]:
    return [value] if jsfmt.is_positive_integer(jsfmt.to_number(value)) else []


def _completed(values: list[str]) -> JsonValue:
    shown: list[JsonValue] = list(values[:100])
    return {"completion": {"values": shown, "total": len(values), "hasMore": False}}


_NOTHING: Final[JsonObject] = {"completion": {"values": [], "hasMore": False}}


async def completion_complete(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """Argument completions for the prompts and resource templates that offer them."""
    ref = params.get("ref") if isinstance(params.get("ref"), dict) else {}
    assert isinstance(ref, dict)  # noqa: S101
    argument = params.get("argument") if isinstance(params.get("argument"), dict) else {}
    assert isinstance(argument, dict)  # noqa: S101
    name, value = str(argument.get("name")), str(argument.get("value", ""))
    context = params.get("context") if isinstance(params.get("context"), dict) else {}
    assert isinstance(context, dict)  # noqa: S101
    given = context.get("arguments") if isinstance(context.get("arguments"), dict) else {}
    assert isinstance(given, dict)  # noqa: S101
    if ref.get("type") == "ref/prompt":
        prompt = str(ref.get("name"))
        if prompt not in _PROMPT_ARGS:
            raise RpcError(-32602, f"MCP error -32602: Prompt {prompt} not found")
        return _prompt_values(prompt, name, value, given)
    if ref.get("type") == "ref/resource" and isinstance(ref.get("uri"), str):
        uri = str(ref["uri"])
        if uri in _TEMPLATES:
            return _completed(_resource_id_values(value)) if name == "resourceId" else _NOTHING
        docs = assets()["docs"]
        assert isinstance(docs, dict)  # noqa: S101
        if uri in docs or uri in mcp_session(ctx).resources:
            return _NOTHING
        raise RpcError(-32602, f"MCP error -32602: Resource template {uri} not found")
    raise RpcError(-32603, _BAD_REF)


def _prompt_values(prompt: str, name: str, value: str, given: dict[str, JsonValue]) -> JsonValue:
    if prompt == "completable-prompt" and name == "department":
        return _completed([d for d in _DEPARTMENTS if d.startswith(value)])
    if prompt == "completable-prompt" and name == "name":
        team = _TEAMS.get(str(given.get("department")), ())
        return _completed([member for member in team if member.startswith(value)])
    if prompt == "resource-prompt" and name == "resourceType":
        return _completed([kind for kind in ("Text", "Blob") if kind.startswith(value)])
    if prompt == "resource-prompt" and name == "resourceId":
        return _completed(_resource_id_values(value))
    return _NOTHING


# -- client notifications --------------------------------------------------------------------------


async def initialized(ctx: RequestContext, _params: JsonObject) -> JsonValue:
    """350 ms after ``initialized``, a client that has roots is asked for them (GET stream).

    Lost when the client has no GET stream open by then: the tool asks again later.
    """
    if "roots" not in ctx.client_capabilities:
        return None
    await ctx.pause(ROOTS_DELAY)
    session = mcp_session(ctx)
    if session.roots is None:
        session.roots_watch = True
        await request_roots(ctx, wait=False)
    return None


async def roots_list_changed(ctx: RequestContext, _params: JsonObject) -> JsonValue:
    """The client's roots changed: ask again (once the server first asked)."""
    if mcp_session(ctx).roots_watch:
        await request_roots(ctx)
    return None


METHOD_HANDLERS: Final[dict[str, MethodHandler]] = {
    "resources/list": resources_list,
    "resources/read": resources_read,
    "resources/subscribe": resources_subscribe,
    "resources/unsubscribe": resources_unsubscribe,
    "logging/setLevel": logging_set_level,
    "prompts/get": prompts_get,
    "completion/complete": completion_complete,
    "tasks/get": research.tasks_get,
    "tasks/list": research.tasks_list,
    "tasks/cancel": research.tasks_cancel,
    "tasks/result": research.tasks_result,
    "notifications/initialized": initialized,
    "notifications/roots/list_changed": roots_list_changed,
}

__all__ = ["METHOD_HANDLERS"]
