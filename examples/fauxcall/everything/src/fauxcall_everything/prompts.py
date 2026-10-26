"""Prompts and argument completions, computed as the server's prompt callbacks compute them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fauxcall.runtime import RpcError

from fauxcall_everything import jsfmt, zod
from fauxcall_everything.common import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    client_state,
    mcp_error_text,
    protocol_error,
)
from fauxcall_everything.resources import STATIC_URI
from fauxcall_everything.tools import RESOURCE_TYPES, dynamic_resource

if TYPE_CHECKING:
    from collections.abc import Callable

    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

DEPARTMENTS: Final = ("Engineering", "Sales", "Marketing", "Support")
TEAM: Final = {
    "Engineering": ("Alice", "Bob", "Charlie"),
    "Sales": ("David", "Eve", "Frank"),
    "Marketing": ("Grace", "Henry", "Iris"),
    "Support": ("John", "Kim", "Lee"),
}
PROMPT_ARGS: Final[dict[str, list[zod.Field]]] = {
    "args-prompt": [zod.Field("city", "string"), zod.Field("state", "string", optional=True)],
    "completable-prompt": [zod.Field("department", "string"), zod.Field("name", "string")],
    "resource-prompt": [zod.Field("resourceType", "string"), zod.Field("resourceId", "string")],
}
"""Prompts with an argument schema (``simple-prompt`` has none and ignores what it gets)."""
TEMPLATES: Final = (
    "demo://resource/dynamic/text/{resourceId}",
    "demo://resource/dynamic/blob/{resourceId}",
)
EMPTY_COMPLETION: Final[JsonObject] = {"completion": {"values": [], "hasMore": False}}
MAX_COMPLETIONS: Final = 100


def _user(content: JsonObject) -> JsonObject:
    return {"role": "user", "content": content}


def _text(value: str) -> JsonObject:
    return {"type": "text", "text": value}


def _prompt_arguments(params: JsonObject) -> JsonObject:
    """``params.arguments``, refused as the SDK's schema refuses non-string values."""
    arguments = params.get("arguments")
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        issue = zod.invalid_type("record", arguments, ["params", "arguments"])
        raise protocol_error([issue])
    issues = [
        zod.invalid_type("string", value, ["params", "arguments", key])
        for key, value in arguments.items()
        if not isinstance(value, str)
    ]
    if issues:
        raise protocol_error(issues)
    return arguments


def _resource_prompt(ctx: RequestContext, args: JsonObject) -> JsonValue:
    kind, raw_id = str(args["resourceType"]), str(args["resourceId"])
    if kind not in RESOURCE_TYPES:
        raise RpcError(INTERNAL_ERROR, f"Invalid resourceType: {kind}. Must be Text or Blob.")
    resource_id = jsfmt.to_number(raw_id)
    if not jsfmt.is_positive_integer(resource_id):
        raise RpcError(
            INTERNAL_ERROR, f"Invalid resourceId: {raw_id}. Must be a finite positive integer."
        )
    resource = dynamic_resource(kind, resource_id, ctx.now_ns)
    intro = (
        f"This prompt includes the {kind} resource with id: {jsfmt.number_text(resource_id)}. "
        "Please analyze the following resource:"
    )
    return {"messages": [_user(_text(intro)), _user({"type": "resource", "resource": resource})]}


async def get_prompt(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``prompts/get``."""
    name = params.get("name", jsfmt.UNDEFINED)
    if not isinstance(name, str):
        raise protocol_error([zod.invalid_type("string", name, ["params", "name"])])
    arguments = _prompt_arguments(params)
    if name == "simple-prompt":
        return {"messages": [_user(_text("This is a simple prompt without arguments."))]}
    fields = PROMPT_ARGS.get(name)
    if fields is None:
        raise RpcError(INVALID_PARAMS, mcp_error_text(INVALID_PARAMS, f"Prompt {name} not found"))
    args, issues = zod.parse(fields, arguments)
    if issues:
        message = f"Invalid arguments for prompt {name}: {zod.sdk_message(issues)}"
        raise RpcError(INVALID_PARAMS, mcp_error_text(INVALID_PARAMS, message))
    if name == "args-prompt":
        state = args.get("state")
        location = str(args["city"]) + (f", {state}" if state else "")
        return {"messages": [_user(_text(f"What's weather in {location}?"))]}
    if name == "completable-prompt":
        line = f"Please promote {args['name']} to the head of the {args['department']} team."
        return {"messages": [_user(_text(line))]}
    return _resource_prompt(ctx, args)


def _positive_id(value: str, _context: JsonObject) -> list[str]:
    number = jsfmt.to_number(value)
    return [value] if number.is_integer() and number > 0 else []


def _starting(options: tuple[str, ...]) -> Callable[[str, JsonObject], list[str]]:
    return lambda value, _context: [option for option in options if option.startswith(value)]


def _team_member(value: str, context: JsonObject) -> list[str]:
    arguments = context.get("arguments") if isinstance(context, dict) else None
    department = arguments.get("department") if isinstance(arguments, dict) else None
    members = TEAM.get(department) if isinstance(department, str) else None
    return [name for name in members or () if name.startswith(value)]


PROMPT_COMPLETERS: Final[dict[str, dict[str, Callable[[str, JsonObject], list[str]]]]] = {
    "completable-prompt": {"department": _starting(DEPARTMENTS), "name": _team_member},
    "resource-prompt": {"resourceType": _starting(RESOURCE_TYPES), "resourceId": _positive_id},
}


def _completion(values: list[str]) -> JsonObject:
    shown: list[JsonValue] = list(values[:MAX_COMPLETIONS])
    return {
        "completion": {
            "values": shown,
            "total": len(values),
            "hasMore": len(values) > MAX_COMPLETIONS,
        }
    }


REFERENCES: Final = (("ref/prompt", "name"), ("ref/resource", "uri"))
"""``ref`` is a prompt or a resource-template reference (a zod union, members in this order)."""


def _reference_issues(ref: JsonValue | jsfmt.Undefined) -> list[zod.Issue]:
    """The union's refusal of ``ref``: every option's issues, or nothing when one matches."""
    options: list[JsonValue] = []
    for literal, key in REFERENCES:
        if not isinstance(ref, dict):
            found = [zod.invalid_type("object", ref, [])]
        else:
            found = [] if ref.get("type") == literal else [zod.invalid_value([literal], ["type"])]
            if not isinstance(ref.get(key), str):
                found.append(zod.invalid_type("string", ref.get(key, jsfmt.UNDEFINED), [key]))
        if not found:
            return []
        options.append([issue.as_json() for issue in found])
    union: dict[str, JsonValue] = {
        "code": "invalid_union",
        "errors": options,
        "message": "Invalid input",
    }
    return [zod.Issue(union, ["params", "ref"])]


def _string_members(
    value: JsonObject, keys: tuple[str, ...], path: list[str | int]
) -> list[zod.Issue]:
    return [
        zod.invalid_type("string", value.get(key, jsfmt.UNDEFINED), [*path, key])
        for key in keys
        if not isinstance(value.get(key), str)
    ]


def _complete_issues(params: JsonObject) -> list[zod.Issue]:
    """``CompleteRequestParamsSchema``: ``ref``, ``argument``, ``context``, in that order."""
    issues = _reference_issues(params.get("ref", jsfmt.UNDEFINED))
    argument = params.get("argument", jsfmt.UNDEFINED)
    if not isinstance(argument, dict):
        issues.append(zod.invalid_type("object", argument, ["params", "argument"]))
    else:
        issues += _string_members(argument, ("name", "value"), ["params", "argument"])
    if "context" in params:
        context = params["context"]
        if not isinstance(context, dict):
            issues.append(zod.invalid_type("object", context, ["params", "context"]))
        elif "arguments" in context:
            arguments = context["arguments"]
            path: list[str | int] = ["params", "context", "arguments"]
            if not isinstance(arguments, dict):
                issues.append(zod.invalid_type("record", arguments, path))
            else:
                issues += _string_members(arguments, tuple(arguments), path)
    return issues


async def complete(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``completion/complete`` for prompt arguments and template variables."""
    issues = _complete_issues(params)
    if issues:
        raise protocol_error(issues)
    ref, argument = params["ref"], params["argument"]
    assert isinstance(ref, dict)  # noqa: S101 - checked above
    assert isinstance(argument, dict)  # noqa: S101 - checked above
    name, value = str(argument["name"]), str(argument["value"])
    context = params.get("context")
    context_obj: JsonObject = context if isinstance(context, dict) else {}
    if ref.get("type") == "ref/prompt":
        prompt = ref.get("name")
        if prompt in {"simple-prompt", "args-prompt"}:
            return EMPTY_COMPLETION
        completers = PROMPT_COMPLETERS.get(str(prompt))
        if completers is None:
            message = mcp_error_text(INVALID_PARAMS, f"Prompt {prompt} not found")
            raise RpcError(INVALID_PARAMS, message)
        completer = completers.get(name)
        return EMPTY_COMPLETION if completer is None else _completion(completer(value, context_obj))
    uri = str(ref.get("uri"))
    if uri in TEMPLATES:
        return (
            _completion(_positive_id(value, context_obj))
            if name == "resourceId"
            else EMPTY_COMPLETION
        )
    if uri.startswith(STATIC_URI) and uri in {
        str(r.get("uri"))
        for r in (ctx.model.mcp.resources if ctx.model.mcp else [])
        if isinstance(r, dict)
    }:
        return EMPTY_COMPLETION
    if uri in client_state(ctx).resources:
        return EMPTY_COMPLETION
    message = mcp_error_text(INVALID_PARAMS, f"Resource template {uri} not found")
    raise RpcError(INVALID_PARAMS, message)
