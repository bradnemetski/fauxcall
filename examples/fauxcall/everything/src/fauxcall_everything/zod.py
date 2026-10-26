"""The real server's argument validation (zod v4 in the MCP TypeScript SDK), message for message.

A tool's arguments are parsed against its zod object schema: fields in their declared order,
defaults filled in, unknown keys dropped. Each failure is an issue; the SDK joins the issues'
messages with newlines (``"Invalid input: expected number, received string at a"``) or, for
protocol-level checks, writes them as ``JSON.stringify(issues, null, 2)``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from fauxcall_everything import jsfmt, whatwg

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fauxcall.types import JsonObject, JsonValue

Kind = Literal["string", "number", "boolean", "enum", "url"]
_MISSING: Final = object()


@dataclass(frozen=True, slots=True)
class Field:
    """One field of a zod object schema."""

    name: str
    kind: Kind
    values: tuple[str, ...] = ()
    default: object = _MISSING
    optional: bool = False
    minimum: float | None = None
    maximum: float | None = None


@dataclass(slots=True)
class Issue:
    """One zod issue, with the members zod writes, in its order."""

    members: dict[str, JsonValue]
    path: list[str | int] = field(default_factory=list)

    @property
    def message(self) -> str:
        """The issue's message."""
        return str(self.members["message"])

    def as_json(self) -> JsonObject:
        """The issue as ``JSON.stringify`` writes it (path, then message, last)."""
        members = {key: value for key, value in self.members.items() if key != "message"}
        return {**members, "path": list(self.path), "message": self.message}


def received(value: JsonValue | object) -> str:
    """The name zod gives a value's type (``util.parsedType``)."""
    if value is _MISSING or isinstance(value, jsfmt.Undefined):
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "nan" if isinstance(value, float) and math.isnan(value) else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def invalid_type(expected: str, value: JsonValue | object, path: Sequence[str | int]) -> Issue:
    """``Invalid input: expected <type>, received <type>``."""
    message = f"Invalid input: expected {expected}, received {received(value)}"
    return Issue({"expected": expected, "code": "invalid_type", "message": message}, list(path))


def invalid_value(values: Sequence[str], path: Sequence[str | int]) -> Issue:
    """``Invalid option: expected one of "a"|"b"`` (an enum)."""
    if len(values) == 1:
        message = f'Invalid input: expected "{values[0]}"'
    else:
        message = "Invalid option: expected one of " + "|".join(f'"{v}"' for v in values)
    return Issue({"code": "invalid_value", "values": list(values), "message": message}, list(path))


def _bound(kind: Literal["too_small", "too_big"], limit: float, path: list[str | int]) -> Issue:
    comparison = ">=" if kind == "too_small" else "<="
    adjective = "Too small" if kind == "too_small" else "Too big"
    bound = "minimum" if kind == "too_small" else "maximum"
    text = jsfmt.number_text(limit)
    return Issue(
        {
            "origin": "number",
            "code": kind,
            bound: jsfmt.json_number(limit),
            "inclusive": True,
            "message": f"{adjective}: expected number to be {comparison}{text}",
        },
        path,
    )


def _check_text(spec: Field, value: JsonValue) -> tuple[JsonValue, list[Issue]]:
    """A string, or a URL (trimmed, tabs and newlines removed, as ``z.url()`` passes it on)."""
    path: list[str | int] = [spec.name]
    if not isinstance(value, str):
        return value, [invalid_type("string", value, path)]
    if spec.kind != "url":
        return value, []
    trimmed = value.strip(jsfmt.JS_WHITESPACE)
    if not whatwg.is_valid_url(trimmed):
        url_issue: dict[str, JsonValue] = {
            "origin": "string",
            "code": "invalid_format",
            "format": "url",
            "message": "Invalid URL",
        }
        return value, [Issue(url_issue, path)]
    return re.sub(r"[\t\n\r]", "", trimmed), []


def _check_number(spec: Field, value: JsonValue) -> tuple[JsonValue, list[Issue]]:
    """A finite number within the field's bounds."""
    path: list[str | int] = [spec.name]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value, [invalid_type("number", value, path)]
    number = jsfmt.to_float(value)
    issues: list[Issue] = []
    if not math.isfinite(number):
        issues.append(invalid_type("number", number, path))
    if spec.minimum is not None and number < spec.minimum:
        issues.append(_bound("too_small", spec.minimum, path))
    if spec.maximum is not None and number > spec.maximum:
        issues.append(_bound("too_big", spec.maximum, path))
    return value, issues


def _check(spec: Field, value: JsonValue) -> tuple[JsonValue, list[Issue]]:
    """One present value against its field: the parsed value and its issues."""
    if spec.kind == "enum":
        ok = isinstance(value, str) and value in spec.values
        return value, [] if ok else [invalid_value(spec.values, [spec.name])]
    if spec.kind in {"string", "url"}:
        return _check_text(spec, value)
    if spec.kind == "boolean":
        ok = isinstance(value, bool)
        return value, [] if ok else [invalid_type("boolean", value, [spec.name])]
    return _check_number(spec, value)


def parse(fields: Sequence[Field], arguments: JsonObject) -> tuple[JsonObject, list[Issue]]:
    """Parse arguments as zod parses an object: (data with defaults, issues in field order)."""
    data: JsonObject = {}
    issues: list[Issue] = []
    for spec in fields:
        value = arguments.get(spec.name, _MISSING)
        if value is _MISSING:
            if spec.default is not _MISSING:
                data[spec.name] = spec.default  # type: ignore[assignment]
            elif not spec.optional:
                if spec.kind == "enum":
                    issues.append(invalid_value(spec.values, [spec.name]))
                else:
                    expected = "string" if spec.kind == "url" else spec.kind
                    issues.append(invalid_type(expected, value, [spec.name]))
            continue
        parsed, found = _check(spec, value)  # type: ignore[arg-type]
        issues += found
        data[spec.name] = parsed
    return data, issues


def dot_path(path: Sequence[str | int]) -> str:
    """The SDK's ``getDotPath``: ``a.b[0].c``."""
    text = ""
    for index, part in enumerate(path):
        if index == 0:
            text = str(part)
        elif isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}"
    return text


def sdk_message(issues: Sequence[Issue]) -> str:
    """The SDK's ``getParseErrorMessage``: each issue's message ``at`` its path, one per line."""
    return "\n".join(
        issue.message if not issue.path else f"{issue.message} at {dot_path(issue.path)}"
        for issue in issues
    )


def issues_json(issues: Sequence[Issue]) -> str:
    """A ZodError's message: ``JSON.stringify(issues, null, 2)``."""
    return jsfmt.stringify([issue.as_json() for issue in issues])


def tool_validation_error(tool: str, issues: Sequence[Issue]) -> str:
    """The text of the ``isError`` result a tool call with invalid arguments gets."""
    return (
        "MCP error -32602: Input validation error: "
        f"Invalid arguments for tool {tool}: {sdk_message(issues)}"
    )
