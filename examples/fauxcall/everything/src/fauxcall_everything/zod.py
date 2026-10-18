"""The validation errors of the real server's zod v4 schemas, word for word.

The TypeScript SDK reports them two ways, both recorded:

* tool arguments: ``MCP error -32602: Input validation error: Invalid arguments for tool echo:
  Invalid input: expected string, received undefined at message`` (issues joined by newlines,
  each with ``at <path>``), as an ``isError`` result;
* malformed requests and client answers: the issues themselves, as ``JSON.stringify(issues,
  null, 2)`` with each issue's members in zod's order (``expected, code, path, message`` for a
  wrong type; ``code, values, path, message`` for a value outside an enum).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from fauxcall_everything import js

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fauxcall.types import JsonObject, JsonValue


class _Missing:
    """``undefined``: a member that isn't there at all."""

    def __repr__(self) -> str:
        return "undefined"


MISSING: Final = _Missing()
type Path = list[str | int]
type Check = Callable[[object, Path], tuple[object, list[Issue]]]
"""A schema's check: the value (or ``MISSING``) and its path -> the parsed value, its issues."""


@dataclass(slots=True)
class Issue:
    """One zod issue: its leading members (in zod's order), path and message."""

    head: JsonObject
    path: Path
    message: str

    def as_json(self) -> JsonObject:
        """The issue as ``JSON.stringify`` writes it."""
        return {**self.head, "path": list(self.path), "message": self.message}

    def nested(self, prefix: Sequence[str | int]) -> Issue:
        """The same issue, found under ``prefix``."""
        return Issue(self.head, [*prefix, *self.path], self.message)


def received(value: object) -> str:  # noqa: PLR0911 - one return per JS type
    """The name zod gives the type it got (``undefined``, ``null``, ``array`` ...)."""
    if value is MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        number = js.as_number(value)
        return js.js_number(number) if math.isinf(number) else "number"  # "Infinity"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def wrong_type(expected: str, value: object, path: Path) -> Issue:
    """``Invalid input: expected string, received number``."""
    head: JsonObject = {"expected": expected, "code": "invalid_type"}
    if expected == "number" and received(value).endswith("Infinity"):
        head["received"] = received(value)  # z.number() names a non-finite number
    return Issue(
        head,
        path,
        f"Invalid input: expected {expected}, received {received(value)}",
    )


def not_an_object(value: object, path: Path) -> Issue:
    """What a (discriminated) union of objects says about a value that isn't an object."""
    return Issue(
        {"code": "invalid_type", "expected": "object"},
        path,
        f"Invalid input: expected object, received {received(value)}",
    )


def required(path: Path) -> Issue:
    """A required ``z.any()`` member that is missing: ``expected nonoptional``."""
    return Issue(
        {"code": "invalid_type", "expected": "nonoptional"},
        path,
        "Invalid input: expected nonoptional, received undefined",
    )


def union(alternatives: Sequence[Sequence[Issue]], path: Path) -> Issue:
    """``z.union``'s refusal: every alternative's issues (paths relative to the value)."""
    errors: list[JsonValue] = [[issue.as_json() for issue in issues] for issues in alternatives]
    return Issue({"code": "invalid_union", "errors": errors}, path, "Invalid input")


def no_discriminator(options: Sequence[str], path: Path) -> Issue:
    """``z.discriminatedUnion``'s refusal of an unknown (or missing) discriminator value."""
    expected = " | ".join(f"'{option}'" for option in options)
    return Issue(
        {
            "code": "invalid_union",
            "errors": [],
            "note": "No matching discriminator",
            "discriminator": "type",
            "options": list(options),
        },
        path,
        f"Invalid discriminator value. Expected {expected}",
    )


def not_one_of(values: Sequence[str], path: Path) -> Issue:
    """``Invalid option: expected one of "a"|"b"``."""
    options = "|".join(js.js_stringify(v) for v in values)
    return Issue(
        {"code": "invalid_value", "values": list(values)},
        path,
        f"Invalid option: expected one of {options}",
    )


def not_literal(value: str, path: Path) -> Issue:
    """``Invalid input: expected "ref/prompt"``."""
    return Issue(
        {"code": "invalid_value", "values": [value]},
        path,
        f"Invalid input: expected {js.js_stringify(value)}",
    )


def dot_path(path: Path) -> str:
    """The SDK's ``getDotPath``: ``a.b[0]``."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + part
    return out


def tool_message(tool: str, issues: Sequence[Issue]) -> str:
    """The text of the ``isError`` result for arguments that break a tool's schema."""
    detail = "\n".join(
        f"{issue.message} at {dot_path(issue.path)}" if issue.path else issue.message
        for issue in issues
    )
    return f"MCP error -32602: Input validation error: Invalid arguments for tool {tool}: {detail}"


def issues_text(issues: Sequence[Issue]) -> str:
    """``ZodError.message``: the issues as indented JSON."""
    return js.js_stringify([issue.as_json() for issue in issues], 2)


# -- schemas ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class Field:
    """One member of an object schema."""

    check: Check
    default: JsonValue | _Missing = MISSING
    optional: bool = False


def string(*, url: bool = False) -> Check:
    """``z.string()`` (``.url()``: also a URL ``new URL`` can parse; trimmed, as zod returns it)."""

    def check(value: object, path: Path) -> tuple[object, list[Issue]]:
        if not isinstance(value, str):
            return value, [wrong_type("string", value, path)]
        if url:
            trimmed = js.js_trim(value)
            if js.parse_url(trimmed) is None:
                return value, [
                    Issue({"code": "invalid_format", "format": "url"}, path, "Invalid URL")
                ]
            return trimmed.replace("\t", "").replace("\n", "").replace("\r", ""), []
        return value, []

    return check


def number(*, minimum: float | None = None, maximum: float | None = None) -> Check:
    """``z.number()`` with optional ``.min()`` / ``.max()``."""

    def check(value: object, path: Path) -> tuple[object, list[Issue]]:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return value, [wrong_type("number", value, path)]
        amount = js.as_number(value)
        if not math.isfinite(amount):
            return value, [wrong_type("number", value, path)]
        issues: list[Issue] = []
        if minimum is not None and amount < minimum:
            issues.append(
                Issue(
                    {
                        "origin": "number",
                        "code": "too_small",
                        "minimum": minimum,
                        "inclusive": True,
                    },
                    path,
                    f"Too small: expected number to be >={js.js_number(minimum)}",
                )
            )
        if maximum is not None and amount > maximum:
            issues.append(
                Issue(
                    {"origin": "number", "code": "too_big", "maximum": maximum, "inclusive": True},
                    path,
                    f"Too big: expected number to be <={js.js_number(maximum)}",
                )
            )
        return value, issues

    return check


def boolean() -> Check:
    """``z.boolean()``."""

    def check(value: object, path: Path) -> tuple[object, list[Issue]]:
        if not isinstance(value, bool):
            return value, [wrong_type("boolean", value, path)]
        return value, []

    return check


def enum(*values: str) -> Check:
    """``z.enum([...])``: anything else, of any type, is an invalid option."""

    def check(value: object, path: Path) -> tuple[object, list[Issue]]:
        if not isinstance(value, str) or value not in values:
            return value, [not_one_of(values, path)]
        return value, []

    return check


def parse_object(
    fields: dict[str, Field], value: JsonValue | _Missing, path: Path | None = None
) -> tuple[JsonObject, list[Issue]]:
    """``z.object(shape)``: defaults filled in, unknown members dropped, issues in shape order."""
    where: Path = path or []
    if not isinstance(value, dict):
        return {}, [wrong_type("object", value, where)]
    out: JsonObject = {}
    issues: list[Issue] = []
    for name, spec in fields.items():
        given = value.get(name, MISSING)
        if given is MISSING:
            if not isinstance(spec.default, _Missing):
                out[name] = spec.default
                continue
            if spec.optional:
                continue
        parsed, problems = spec.check(given, [*where, name])
        issues += problems
        if not problems and not isinstance(parsed, _Missing):
            out[name] = cast("JsonValue", parsed)
    return out, issues


@dataclass(slots=True)
class ToolSchema:
    """A tool's input schema: what its handler gets, or the real refusal."""

    name: str
    fields: dict[str, Field] = field(default_factory=dict)

    def parse(self, arguments: JsonObject) -> tuple[JsonObject, str | None]:
        """The parsed arguments, and the refusal text when they break the schema."""
        parsed, issues = parse_object(self.fields, arguments)
        return parsed, tool_message(self.name, issues) if issues else None
