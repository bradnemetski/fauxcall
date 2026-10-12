"""Argument validation worded exactly like the real server's Zod 4 schemas.

Two shapes were recorded:

- tool and prompt arguments: one line per issue, ``<message> at <path>``, inside the SDK's
  ``MCP error -32602: Input validation error: Invalid arguments for tool <name>: …``;
- protocol parameters (``params.uri`` …): the issue list itself, ``JSON.stringify(issues,
  null, 2)``, as a ``-32603`` JSON-RPC error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from fauxcall.runtime import RpcError

from fauxcall_everything.jsnum import InvalidUrlError, js_typeof, normalize_url

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fauxcall.types import JsonObject, JsonValue

Kind = Literal["string", "number", "boolean", "enum", "url"]
INTERNAL_ERROR = -32603
INVALID_PARAMS = -32602


@dataclass(frozen=True, slots=True)
class Field:
    """One property of an object schema, in the schema's order."""

    name: str
    kind: Kind
    required: bool = False
    values: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


@dataclass(slots=True)
class Issue:
    """One Zod issue: its JSON form (protocol errors) and its one-line message."""

    path: list[str | int]
    message: str
    detail: dict[str, JsonValue] = field(default_factory=dict)

    def as_json(self, prefix: Sequence[str | int] = ()) -> JsonObject:
        """The issue as ``JSON.stringify`` lists it (``expected``/``code`` first, then path)."""
        body: dict[str, JsonValue] = dict(self.detail)
        body["path"] = [*prefix, *self.path]
        body["message"] = self.message
        return body


def _type_issue(name: str, expected: str, value: JsonValue, *, present: bool) -> Issue:
    received = js_typeof(value, present=present)
    return Issue(
        [name],
        f"Invalid input: expected {expected}, received {received}",
        {"expected": expected, "code": "invalid_type"},
    )


def enum_issue(name: str, values: Sequence[str]) -> Issue:
    """``Invalid option: expected one of "a"|"b"`` (also when the value is missing)."""
    options = "|".join(json.dumps(option) for option in values)
    return Issue(
        [name],
        f"Invalid option: expected one of {options}",
        {"code": "invalid_value", "values": list(values)},
    )


def _enum(spec: Field, value: JsonValue, *, present: bool) -> Issue | None:
    del present
    if isinstance(value, str) and value in spec.values:
        return None
    return enum_issue(spec.name, spec.values)


def _string(spec: Field, value: JsonValue, *, present: bool) -> Issue | None:
    if not isinstance(value, str):
        return _type_issue(spec.name, "string", value, present=present)
    return None


def _url(spec: Field, value: JsonValue, *, present: bool) -> Issue | None:
    if not isinstance(value, str):
        return _type_issue(spec.name, "string", value, present=present)
    try:
        normalize_url(value)
    except InvalidUrlError:
        return Issue([spec.name], "Invalid URL", {"code": "invalid_format"})
    return None


def _boolean(spec: Field, value: JsonValue, *, present: bool) -> Issue | None:
    if isinstance(value, bool):
        return None
    return _type_issue(spec.name, "boolean", value, present=present)


def _bound(limit: float) -> str:
    return str(int(limit)) if float(limit).is_integer() else str(limit)


def _number(spec: Field, value: JsonValue, *, present: bool) -> Issue | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _type_issue(spec.name, "number", value, present=present)
    if spec.minimum is not None and value < spec.minimum:
        return Issue([spec.name], f"Too small: expected number to be >={_bound(spec.minimum)}")
    if spec.maximum is not None and value > spec.maximum:
        return Issue([spec.name], f"Too big: expected number to be <={_bound(spec.maximum)}")
    return None


_CHECKS: Final[dict[Kind, Callable[..., Issue | None]]] = {
    "enum": _enum,
    "string": _string,
    "url": _url,
    "boolean": _boolean,
    "number": _number,
}


def _field_issue(spec: Field, arguments: JsonObject) -> Issue | None:
    present = spec.name in arguments
    if not present and not spec.required:
        return None
    return _CHECKS[spec.kind](spec, arguments.get(spec.name), present=present)


def issues_of(fields: Sequence[Field], arguments: JsonObject) -> list[Issue]:
    """Every issue, in the schema's property order (as Zod reports them)."""
    return [issue for spec in fields if (issue := _field_issue(spec, arguments)) is not None]


def lines(issues: Sequence[Issue]) -> str:
    """``message at path`` per issue, one per line (the SDK's prettified form)."""
    return "\n".join(f"{issue.message} at {'.'.join(map(str, issue.path))}" for issue in issues)


def tool_error(text: str) -> JsonObject:
    """A tool's error result (``isError: true``)."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


def tool_arguments_error(tool: str, issues: Sequence[Issue]) -> JsonObject:
    """How the SDK answers arguments that break a tool's input schema."""
    return tool_error(
        f"MCP error -32602: Input validation error: Invalid arguments for tool {tool}: "
        + lines(issues)
    )


def prompt_arguments_error(prompt: str, issues: Sequence[Issue]) -> RpcError:
    """How the SDK refuses arguments that break a prompt's argument schema."""
    return RpcError(
        INVALID_PARAMS,
        f"MCP error -32602: Invalid arguments for prompt {prompt}: " + lines(issues),
    )


def protocol_error(issues: Sequence[Issue], prefix: Sequence[str | int] = ("params",)) -> RpcError:
    """A request whose params break the protocol schema: the issue list as JSON, ``-32603``."""
    payload = [issue.as_json(prefix) for issue in issues]
    return RpcError(INTERNAL_ERROR, json.dumps(payload, indent=2, ensure_ascii=False))


def require_string(params: JsonObject, name: str) -> str:
    """A protocol parameter that must be a string (``params.uri``, ``params.taskId`` …)."""
    value = params.get(name)
    if isinstance(value, str):
        return value
    raise protocol_error([_type_issue(name, "string", value, present=name in params)])


def object_issue(name: str, value: JsonValue) -> Issue:
    """A protocol parameter that must be an object (``params.task``)."""
    return _type_issue(name, "object", value, present=True)


def record_issue(name: str, value: JsonValue) -> Issue:
    """``arguments`` that is not an object (``expected record``)."""
    return _type_issue(name, "record", value, present=True)


def string_values_issues(arguments: JsonObject) -> list[Issue]:
    """Prompt arguments must all be strings (``params.arguments.<name>``)."""
    return [
        _type_issue(name, "string", value, present=True)
        for name, value in arguments.items()
        if not isinstance(value, str)
    ]
