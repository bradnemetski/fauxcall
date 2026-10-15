"""The real server's validation, worded as zod v4 and the MCP TypeScript SDK word it.

Tool arguments are refused with an ``isError`` result listing one line per problem
(``Invalid input: expected string, received number at message``); the SDK's own request schemas
refuse with a JSON-RPC ``-32603`` whose message is the zod issues as indented JSON; replies the
client sends (sampling results, elicitation results, roots) are checked against the SDK's
schemas, which also reorder their members. Every message here was recorded from the real server.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import ada_url
from fauxcall.runtime import RpcError

from fauxcall_everything.jsvalues import (
    JS_WHITESPACE,
    is_number,
    number_to_string,
    stringify,
    to_double,
    type_name,
)

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue

INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
_ABSENT: Final = object()

Kind = Literal["string", "number", "boolean", "enum", "url"]


@dataclass(frozen=True, slots=True)
class Field:
    """One argument of a tool's input schema."""

    name: str
    kind: Kind
    values: tuple[str, ...] = ()
    default: object = _ABSENT
    minimum: float | None = None
    maximum: float | None = None
    optional: bool = False


def enum_message(values: tuple[str, ...] | list[str]) -> str:
    """Zod's message for a value outside an enum."""
    return "Invalid option: expected one of " + "|".join(f'"{v}"' for v in values)


def type_message(expected: str, value: object, *, present: bool = True) -> str:
    """Zod's message for a value of the wrong type."""
    return f"Invalid input: expected {expected}, received {type_name(value, present=present)}"


def zod_url(value: str) -> str | None:
    """``z.url()``: the trimmed value (tabs and newlines removed) when WHATWG can parse it."""
    trimmed = value.strip(JS_WHITESPACE)
    if not ada_url.check_url(trimmed):
        return None
    return trimmed.replace("\t", "").replace("\n", "").replace("\r", "")


def _check(field: Field, value: object) -> tuple[object, str | None]:  # noqa: PLR0911
    """A present value: (parsed value, problem or None)."""
    match field.kind:
        case "enum":
            ok = isinstance(value, str) and value in field.values
            return value, None if ok else enum_message(field.values)
        case "boolean":
            return value, None if isinstance(value, bool) else type_message("boolean", value)
        case "string":
            return value, None if isinstance(value, str) else type_message("string", value)
        case "url":
            if not isinstance(value, str):
                return value, type_message("string", value)
            parsed = zod_url(value)
            return (value, "Invalid URL") if parsed is None else (parsed, None)
        case _:
            number = to_double(value) if isinstance(value, int | float) else math.nan
            if not is_number(value) or not math.isfinite(number):  # zod refuses ±Infinity
                return value, type_message("number", value)
            if field.minimum is not None and number < field.minimum:
                return (
                    value,
                    f"Too small: expected number to be >={number_to_string(field.minimum)}",
                )
            if field.maximum is not None and number > field.maximum:
                return value, f"Too big: expected number to be <={number_to_string(field.maximum)}"
            return value, None


def parse_arguments(
    fields: tuple[Field, ...], arguments: JsonObject
) -> tuple[JsonObject, list[str]]:
    """Arguments as the tool sees them (defaults filled in, unknown ones dropped), and problems."""
    parsed: JsonObject = {}
    problems: list[str] = []
    for field in fields:
        if field.name not in arguments:
            if field.default is not _ABSENT:
                parsed[field.name] = field.default  # type: ignore[assignment]
            elif not field.optional:
                missing = (
                    enum_message(field.values)
                    if field.kind == "enum"
                    else type_message(
                        "string" if field.kind == "url" else field.kind, None, present=False
                    )
                )
                problems.append(f"{missing} at {field.name}")
            continue
        value, problem = _check(field, arguments[field.name])
        if problem is not None:
            problems.append(f"{problem} at {field.name}")
        else:
            parsed[field.name] = value  # type: ignore[assignment]
    return parsed, problems


def tool_input_error(tool: str, problems: list[str]) -> JsonObject:
    """The ``isError`` result for arguments that break a tool's schema."""
    text = (
        f"MCP error -32602: Input validation error: Invalid arguments for tool {tool}: "
        + "\n".join(problems)
    )
    return tool_error(text)


def tool_error(text: str) -> JsonObject:
    """A tool's error result, as the SDK wraps a thrown error."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


# -- issues as zod reports them (JSON) -----------------------------------------------------------


def type_issue(
    expected: str, value: object, path: list[JsonValue], *, present: bool = True
) -> JsonObject:
    """An ``invalid_type`` issue (for scalar expectations: ``expected`` first).

    A number JavaScript holds as ±Infinity fails ``z.number()``, which also names it in a
    ``received`` member (000518, 000519).
    """
    issue: JsonObject = {"expected": expected, "code": "invalid_type"}
    if expected == "number" and present and isinstance(value, int | float):
        number = to_double(value)
        if math.isinf(number):
            issue["received"] = number_to_string(number)
    issue["path"] = path
    issue["message"] = type_message(expected, value, present=present)
    return issue


def object_issue(value: object, path: list[JsonValue], *, present: bool = True) -> JsonObject:
    """An ``invalid_type`` issue of a discriminated union (``code`` first)."""
    return {
        "code": "invalid_type",
        "expected": "object",
        "path": path,
        "message": type_message("object", value, present=present),
    }


def value_issue(values: list[str], path: list[JsonValue]) -> JsonObject:
    """An ``invalid_value`` issue (an enum)."""
    listed: list[JsonValue] = list(values)
    return {
        "code": "invalid_value",
        "values": listed,
        "path": path,
        "message": enum_message(values),
    }


def union_issue(errors: list[list[JsonObject]], path: list[JsonValue]) -> JsonObject:
    """An ``invalid_union`` issue: every option failed."""
    return {
        "code": "invalid_union",
        "errors": errors,  # type: ignore[dict-item]
        "path": path,
        "message": "Invalid input",
    }


def issues_text(issues: list[JsonObject]) -> str:
    """A ZodError's message: its issues as JSON indented by two spaces."""
    return stringify(issues, 2)  # type: ignore[arg-type]


def protocol_error(issues: list[JsonObject]) -> RpcError:
    """How the SDK refuses a request whose params break the protocol's schema."""
    return RpcError(INTERNAL_ERROR, issues_text(issues))


def require_string(params: JsonObject, name: str) -> str:
    """A required string param, or the SDK's protocol refusal."""
    value = params.get(name, _ABSENT)
    if isinstance(value, str):
        return value
    raise protocol_error(
        [type_issue("string", value, ["params", name], present=value is not _ABSENT)]
    )


# -- replies from the client ---------------------------------------------------------------------

_ROLES: Final = ["user", "assistant"]
_ACTIONS: Final = ["accept", "decline", "cancel"]
_STOP_REASONS: Final = ["endTurn", "stopSequence", "maxTokens"]
_CONTENT_TYPES: Final = ("text", "image", "audio")
_CONTENT_FIELDS: Final = {
    "text": ("type", "text", "annotations", "_meta"),
    "image": ("type", "data", "mimeType", "annotations", "_meta"),
    "audio": ("type", "data", "mimeType", "annotations", "_meta"),
}
_ANNOTATION_FIELDS: Final = ("audience", "priority", "lastModified")


def _loose(reply: JsonObject, known: tuple[str, ...]) -> JsonObject:
    """A loose object as zod outputs it: known members in schema order, then the others."""
    ordered: JsonObject = {name: reply[name] for name in known if name in reply}
    ordered.update({name: value for name, value in reply.items() if name not in known})
    return ordered


def _content_block(value: JsonValue, issues: list[JsonObject]) -> JsonValue:
    """A sampling content block (text, image or audio), as zod outputs it."""
    if not isinstance(value, dict):
        issues.append(object_issue(value, ["content"], present=value is not _ABSENT))
        return value
    kind = value.get("type")
    if kind not in _CONTENT_TYPES:
        issues.append(
            {
                "code": "invalid_union",
                "errors": [],
                "note": "No matching discriminator",
                "discriminator": "type",
                "options": list(_CONTENT_TYPES),
                "path": ["content", "type"],
                "message": "Invalid discriminator value. Expected 'text' | 'image' | 'audio'",
            }
        )
        return value
    members = ("text",) if kind == "text" else ("data", "mimeType")
    issues.extend(
        type_issue("string", value.get(member), ["content", member], present=member in value)
        for member in members
        if not isinstance(value.get(member), str)
    )
    block: JsonObject = {name: value[name] for name in _CONTENT_FIELDS[str(kind)] if name in value}
    annotations = block.get("annotations")
    if isinstance(annotations, dict):
        block["annotations"] = {n: annotations[n] for n in _ANNOTATION_FIELDS if n in annotations}
    return block


def create_message_result(reply: JsonValue) -> tuple[JsonObject, list[JsonObject]]:
    """A sampling result checked against ``CreateMessageResultSchema``: (parsed, issues)."""
    result = reply if isinstance(reply, dict) else {}
    issues: list[JsonObject] = []
    if not isinstance(result.get("model"), str):
        issues.append(
            type_issue("string", result.get("model"), ["model"], present="model" in result)
        )
    stop = result.get("stopReason", _ABSENT)
    if stop is not _ABSENT and not isinstance(stop, str):
        branches = [[value_issue(_STOP_REASONS, [])], [type_issue("string", stop, [])]]
        issues.append(union_issue(branches, ["stopReason"]))
    if result.get("role") not in _ROLES:
        issues.append(value_issue(_ROLES, ["role"]))
    parsed = _loose(result, ("_meta", "model", "stopReason", "role", "content"))
    content = result.get("content", _ABSENT)
    parsed_content = _content_block(content, issues)  # type: ignore[arg-type]
    if "content" in parsed:
        parsed["content"] = parsed_content
    return parsed, issues


def _content_value_issue(value: JsonValue, path: list[JsonValue]) -> JsonObject | None:
    """An elicitation content value: a string, number, boolean or list of strings."""
    if isinstance(value, str | bool) or is_number(value):
        return None
    if isinstance(value, list):
        bad = [
            type_issue("string", item, [index])
            for index, item in enumerate(value)
            if not isinstance(item, str)
        ]
        if not bad:
            return None
        array_errors = bad
    else:
        array_errors = [type_issue("array", value, [])]
    branches = [
        [type_issue("string", value, [])],
        [type_issue("number", value, [])],
        [type_issue("boolean", value, [])],
        array_errors,
    ]
    return union_issue(branches, path)


def elicit_result(reply: JsonValue) -> tuple[JsonObject, list[JsonObject]]:
    """An elicitation result checked against ``ElicitResultSchema``: (parsed, issues)."""
    result = dict(reply) if isinstance(reply, dict) else {}
    issues: list[JsonObject] = []
    if result.get("action") not in _ACTIONS:
        issues.append(value_issue(_ACTIONS, ["action"]))
    if result.get("content", _ABSENT) is None:  # z.preprocess: null is undefined
        del result["content"]
    content = result.get("content", _ABSENT)
    if content is not _ABSENT:
        if not isinstance(content, dict):
            issues.append(type_issue("record", content, ["content"]))
        else:
            for key, value in content.items():
                issue = _content_value_issue(value, ["content", key])
                if issue is not None:
                    issues.append(issue)
    return _loose(result, ("_meta", "action", "content")), issues


def roots_result(reply: JsonValue) -> list[JsonObject] | None:
    """The roots of a ``roots/list`` answer (``None``: ``ListRootsResultSchema`` refuses it)."""
    roots = reply.get("roots") if isinstance(reply, dict) else None
    if not isinstance(roots, list):
        return None
    parsed: list[JsonObject] = []
    for root in roots:
        if not isinstance(root, dict):
            return None
        uri, name = root.get("uri"), root.get("name", _ABSENT)
        if not isinstance(uri, str) or not uri.startswith("file://"):
            return None
        if name is not _ABSENT and not isinstance(name, str):
            return None
        parsed.append({key: root[key] for key in ("uri", "name", "_meta") if key in root})
    return parsed


# -- the async tools' replies ----------------------------------------------------------------------

_SYNC_FIELDS: Final = {
    "sampling": (
        ("role", "string"),
        ("content", "any"),
        ("model", "string"),
        ("stopReason", "string?"),
    ),
    "elicitation": (("action", "string"), ("content", "any?")),
}


def _client_task_issues(reply: JsonValue) -> list[JsonObject]:
    """Why a reply is not ``{task: {taskId, status, pollInterval?, statusMessage?}}``."""
    if not isinstance(reply, dict):
        return [type_issue("object", reply, [])]
    task = reply.get("task", _ABSENT)
    if not isinstance(task, dict):
        return [type_issue("object", task, ["task"], present=task is not _ABSENT)]
    issues: list[JsonObject] = []
    for name, kind, required in (
        ("taskId", "string", True),
        ("status", "string", True),
        ("pollInterval", "number", False),
        ("statusMessage", "string", False),
    ):
        value = task.get(name, _ABSENT)
        if value is _ABSENT and not required:
            continue
        valid = is_number(value) if kind == "number" else isinstance(value, str)
        if not valid:
            issues.append(type_issue(kind, value, ["task", name], present=value is not _ABSENT))
    return issues


def _sync_issues(reply: JsonValue, kind: str) -> list[JsonObject]:
    """Why a reply is not the synchronous result an async tool also accepts."""
    if not isinstance(reply, dict):
        return [type_issue("object", reply, [])]
    issues: list[JsonObject] = []
    for name, expected in _SYNC_FIELDS[kind]:
        value = reply.get(name, _ABSENT)
        optional = expected.endswith("?")
        if value is _ABSENT:
            if optional:
                continue
            if expected == "any":
                issues.append(
                    {
                        "code": "invalid_type",
                        "expected": "nonoptional",
                        "path": [name],
                        "message": "Invalid input: expected nonoptional, received undefined",
                    }
                )
                continue
        if expected.startswith("string") and not isinstance(value, str):
            issues.append(type_issue("string", value, [name], present=value is not _ABSENT))
    return issues


def client_task_reply(
    reply: JsonValue, kind: str
) -> tuple[JsonObject | None, JsonObject, list[JsonObject]]:
    """An async tool's first reply: (the client's task, else the synchronous answer, issues).

    The tool parses it with ``z.union([{task: …}, <synchronous result>])``: the first option that
    fits wins, and when none does zod reports both options' issues.
    """
    task_issues = _client_task_issues(reply)
    if not task_issues:
        assert isinstance(reply, dict)  # noqa: S101 - checked above
        task = reply["task"]
        assert isinstance(task, dict)  # noqa: S101
        return task, {}, []
    sync_issues = _sync_issues(reply, kind)
    if not sync_issues:
        assert isinstance(reply, dict)  # noqa: S101
        names = [name for name, _ in _SYNC_FIELDS[kind]]
        return None, {name: reply[name] for name in names if name in reply}, []
    return None, {}, [union_issue([task_issues, sync_issues], [])]


def poll_reply(reply: JsonValue) -> tuple[str, str | None, list[JsonObject]]:
    """A client's tasks/get answer, as ``z.looseObject({status, statusMessage?})`` reads it."""
    if not isinstance(reply, dict):
        return "", None, [type_issue("object", reply, [])]
    issues: list[JsonObject] = []
    status = reply.get("status", _ABSENT)
    if not isinstance(status, str):
        issues.append(type_issue("string", status, ["status"], present=status is not _ABSENT))
    message = reply.get("statusMessage", _ABSENT)
    if message is not _ABSENT and not isinstance(message, str):
        issues.append(type_issue("string", message, ["statusMessage"]))
    return (
        status if isinstance(status, str) else "",
        message if isinstance(message, str) else None,
        issues,
    )
