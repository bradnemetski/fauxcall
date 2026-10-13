"""Argument validation that reports problems in the real server's words (Zod 4 messages).

The Everything server validates tool and prompt arguments with Zod schemas; the MCP SDK joins the
issues with newlines: ``Invalid input: expected number, received undefined at a``, ``Invalid
option: expected one of "error"|"success"|"debug" at messageType``, ``Too small: expected number
to be >=1 at count``, ``Invalid URL at data``. Fields are checked in schema order.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from fauxcall_everything.jsnum import as_double

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fauxcall.types import JsonObject, JsonValue

Kind = Literal["string", "number", "boolean", "enum", "url"]

_MISSING: Final = object()
_SCHEME: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_SPECIAL_SCHEMES: Final = frozenset({"http", "https", "ws", "wss", "ftp"})


@dataclass(frozen=True)
class Field:
    """One argument of a Zod object schema."""

    name: str
    kind: Kind
    default: JsonValue | object = _MISSING
    optional: bool = False
    options: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


@dataclass
class Checked:
    """The parsed arguments (defaults applied) and the problems found, in order."""

    values: dict[str, JsonValue] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every argument passed."""
        return not self.issues

    def message(self) -> str:
        """The issues as the SDK prints them (one per line)."""
        return "\n".join(self.issues)


def received(value: JsonValue | object) -> str:  # noqa: PLR0911 - one return per type
    """Zod's name for a value's type."""
    if value is _MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def is_url(text: str) -> bool:
    """Whether WHATWG ``new URL(text)`` accepts it (what ``z.url()`` checks)."""
    trimmed = text.strip()
    match = _SCHEME.match(trimmed)
    if match is None:
        return False
    scheme = match.group(0)[:-1].lower()
    rest = trimmed[match.end() :]
    if scheme in _SPECIAL_SCHEMES or scheme == "file":
        host = rest.lstrip("/\\").split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        return scheme == "file" or bool(host.rsplit("@", 1)[-1].split(":", 1)[0])
    return True


def _issue(spec: Field, value: JsonValue | object) -> str | None:  # noqa: PLR0911
    """The problem with one value, if any (without the ``at <name>`` suffix)."""
    if spec.kind == "enum":
        if isinstance(value, str) and value in spec.options:
            return None
        choices = "|".join(f'"{option}"' for option in spec.options)
        return f"Invalid option: expected one of {choices}"
    expected = "string" if spec.kind == "url" else spec.kind
    if received(value) != expected:
        return f"Invalid input: expected {expected}, received {received(value)}"
    if spec.kind == "number" and not math.isfinite(as_double(value)):  # type: ignore[arg-type]
        return "Invalid input: expected number, received Infinity"  # recorded (a JSON 1e400)
    if spec.kind == "url" and isinstance(value, str) and not is_url(value):
        return "Invalid URL"
    if isinstance(value, int | float) and not isinstance(value, bool):
        if spec.minimum is not None and value < spec.minimum:
            return f"Too small: expected number to be >={_bound(spec.minimum)}"
        if spec.maximum is not None and value > spec.maximum:
            return f"Too big: expected number to be <={_bound(spec.maximum)}"
    return None


def _bound(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def check(arguments: JsonObject, fields: Sequence[Field]) -> Checked:
    """Parse ``arguments`` against the fields like ``z.object({...}).safeParse``."""
    checked = Checked()
    for spec in fields:
        value: JsonValue | object = arguments.get(spec.name, _MISSING)
        if value is _MISSING:
            if spec.default is not _MISSING:
                checked.values[spec.name] = spec.default  # type: ignore[assignment]
                continue
            if spec.optional:
                continue
        problem = _issue(spec, value)
        if problem is not None:
            checked.issues.append(f"{problem} at {spec.name}")
        else:
            checked.values[spec.name] = value  # type: ignore[assignment]
    return checked


def tool_error_text(tool: str, checked: Checked) -> str:
    """The text of the ``isError`` result the SDK answers for invalid tool arguments."""
    return (
        f"MCP error -32602: Input validation error: Invalid arguments for tool {tool}: "
        f"{checked.message()}"
    )


def prompt_error_message(prompt: str, checked: Checked) -> str:
    """The JSON-RPC error message the SDK answers for invalid prompt arguments."""
    return f"MCP error -32602: Invalid arguments for prompt {prompt}: {checked.message()}"


def protocol_issues(issues: Sequence[JsonObject]) -> str:
    """A protocol-level schema failure (``-32603``): the Zod issues as pretty-printed JSON."""
    return json.dumps(list(issues), indent=2, ensure_ascii=False)


def missing_string_param(name: str, value: JsonValue | object = _MISSING) -> str:
    """The ``-32603`` message for a request param that isn't a string (``params.uri`` ...)."""
    return protocol_issues(
        [
            {
                "expected": "string",
                "code": "invalid_type",
                "path": ["params", name],
                "message": f"Invalid input: expected string, received {received(value)}",
            }
        ]
    )


MISSING: Final = _MISSING
"""The marker for an argument that wasn't sent."""
