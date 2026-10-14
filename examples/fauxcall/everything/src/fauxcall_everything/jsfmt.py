"""How the real server (Node.js + zod v4) spells values: numbers, strings, JSON and URLs.

The real server is JavaScript: its texts print numbers with ``Number.prototype.toString``, its
``JSON.stringify(value, null, 2)`` differs from Python's ``json.dumps`` for floats, its argument
checks are zod v4's (with zod's exact messages), and resource URIs go through WHATWG ``new URL``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue

_SPECIAL_SCHEMES: Final = frozenset({"http", "https", "ws", "wss", "ftp"})
_SCHEME: Final = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):(.*)$", re.DOTALL)
_HOST_FORBIDDEN: Final = re.compile(r"[\s#%/:<>?@\[\\\]^|]")
_JS_WHITESPACE: Final = (
    " \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_DECIMAL: Final = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_RADIX: Final = {"0x": 16, "0o": 8, "0b": 2}
_EXPONENT_LIMIT: Final = 21
_SMALL_LIMIT: Final = -6


def number(value: float) -> str:  # noqa: PLR0911
    """``String(value)`` for a JavaScript number (ECMAScript ``Number::toString``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # the shortest round-tripping digits (Python's repr and JavaScript agree on them)
    shortest = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(str(d) for d in shortest.digits)
    exponent = shortest.exponent
    assert isinstance(exponent, int)  # noqa: S101 - finite numbers only here
    k = len(digits)
    point = k + exponent  # ECMAScript's n: where the decimal point sits after the digits
    if k <= point <= _EXPONENT_LIMIT:
        return sign + digits + "0" * (point - k)
    if 0 < point <= _EXPONENT_LIMIT:
        return sign + digits[:point] + "." + digits[point:]
    if _SMALL_LIMIT < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    shown = point - 1
    mark = "+" if shown >= 0 else "-"
    head = digits[0] + ("." + digits[1:] if k > 1 else "")
    return f"{sign}{head}e{mark}{abs(shown)}"


def to_string(value: JsonValue) -> str:
    """``String(value)`` / template-literal interpolation of a JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return number(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None else to_string(item) for item in value)
    return "[object Object]"


def to_number(text: str) -> float:  # noqa: PLR0911
    """``Number(text)``: JavaScript's string-to-number conversion."""
    stripped = text.strip(_JS_WHITESPACE)
    if not stripped:
        return 0.0
    radix = _RADIX.get(stripped[:2].lower())
    if radix is not None:
        try:
            return float(int(stripped[2:], radix))
        except ValueError:
            return math.nan
    if stripped in {"Infinity", "+Infinity"}:
        return math.inf
    if stripped == "-Infinity":
        return -math.inf
    if not _DECIMAL.match(stripped):
        return math.nan
    return float(stripped)


def is_positive_integer(value: float) -> bool:
    """``Number.isFinite(v) && Number.isInteger(v) && v > 0``."""
    return math.isfinite(value) and value == int(value) and value > 0


def stringify(value: JsonValue, indent: int = 2) -> str:
    """``JSON.stringify(value, null, indent)``."""
    return _stringify(value, indent, 0)


def _stringify(value: JsonValue, indent: int, depth: int) -> str:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int | float):
        return number(value) if math.isfinite(value) else "null"
    pad = " " * (indent * (depth + 1))
    end = " " * (indent * depth)
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [pad + _stringify(item, indent, depth + 1) for item in value]
        return "[\n" + ",\n".join(items) + "\n" + end + "]"
    if not value:
        return "{}"
    members = [
        f"{pad}{json.dumps(key, ensure_ascii=False)}: {_stringify(item, indent, depth + 1)}"
        for key, item in value.items()
    ]
    return "{\n" + ",\n".join(members) + "\n" + end + "}"


def is_url(text: str) -> bool:
    """Whether WHATWG ``new URL(text)`` accepts ``text`` (zod's ``z.url()`` check)."""
    match = _SCHEME.match(text.strip())
    if match is None:
        return False
    scheme, rest = match.group(1).lower(), match.group(2)
    if scheme not in _SPECIAL_SCHEMES:
        return True
    authority = re.split(r"[/?#]", rest.lstrip("/\\"), maxsplit=1)[0]
    host_port = authority.rsplit("@", 1)[-1]
    if host_port.startswith("["):  # an IPv6 literal
        literal, closed, after = host_port[1:].partition("]")
        valid = bool(closed) and bool(re.fullmatch(r"[0-9A-Fa-f:.]+", literal))
        port = after.removeprefix(":")
        return valid and (not port or (port.isdigit() and int(port) <= 65535))  # noqa: PLR2004
    host, _, port = host_port.partition(":")
    if not host or _HOST_FORBIDDEN.search(host):
        return False
    return not port or (port.isdigit() and int(port) <= 65535)  # noqa: PLR2004


def href(uri: str) -> str:
    """``new URL(uri).toString()`` for the URIs this server serves (scheme lowered, spaces …)."""
    match = _SCHEME.match(uri.strip(_JS_WHITESPACE))
    if match is None:
        return uri
    rest = match.group(2)
    for raw, encoded in ((" ", "%20"), ('"', "%22"), ("<", "%3C"), (">", "%3E"), ("`", "%60")):
        rest = rest.replace(raw, encoded)
    return match.group(1).lower() + ":" + rest


def received(value: object, *, present: bool = True) -> str:  # noqa: PLR0911
    """The name zod v4 gives for what it received."""
    if not present:
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


@dataclass(slots=True)
class Field:
    """One argument of a zod object schema."""

    kind: str  # string | number | boolean | enum | url
    required: bool = False
    default: JsonValue = None
    options: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    issues: list[str] = field(default_factory=list)


def check(
    schema: dict[str, Field], arguments: dict[str, JsonValue]
) -> tuple[dict[str, JsonValue], list[str]]:
    """Parse ``arguments`` like ``z.object(schema)``: the parsed values and zod's messages."""
    parsed: dict[str, JsonValue] = {}
    issues: list[str] = []
    for name, spec in schema.items():
        present = name in arguments
        value = arguments.get(name)
        if not present and not spec.required:
            if spec.default is not None:
                parsed[name] = spec.default
            continue
        problem = _problem(spec, value, present=present)
        if problem is not None:
            issues.append(f"{problem} at {name}")
            continue
        if spec.kind == "url" and isinstance(value, str):
            value = re.sub(r"[\t\n\r]", "", value.strip())
        parsed[name] = value
    return parsed, issues


def _problem(spec: Field, value: JsonValue, *, present: bool) -> str | None:  # noqa: PLR0911
    if spec.kind == "enum":
        if value in spec.options and isinstance(value, str):
            return None
        quoted = "|".join(f'"{option}"' for option in spec.options)
        return f"Invalid option: expected one of {quoted}"
    expected = "string" if spec.kind == "url" else spec.kind
    got = received(value, present=present)
    if got != expected:
        return f"Invalid input: expected {expected}, received {got}"
    if isinstance(value, float) and not math.isfinite(value):
        return f"Invalid input: expected number, received {number(value)}"
    if spec.kind == "url" and isinstance(value, str) and not is_url(value):
        return "Invalid URL"
    if spec.kind == "number" and isinstance(value, int | float) and not isinstance(value, bool):
        if spec.minimum is not None and value < spec.minimum:
            return f"Too small: expected number to be >={number(spec.minimum)}"
        if spec.maximum is not None and value > spec.maximum:
            return f"Too big: expected number to be <={number(spec.maximum)}"
    return None


def issue_json(path: list[JsonValue], expected: str, got: str) -> str:
    """The pretty-printed zod issue list the SDK answers malformed protocol params with."""
    issue: JsonObject = {
        "expected": expected,
        "code": "invalid_type",
        "path": list(path),
        "message": f"Invalid input: expected {expected}, received {got}",
    }
    return stringify([issue])


def as_float(value: JsonValue) -> float:
    """A number zod already checked, as a float (JavaScript numbers are doubles)."""
    return float(value) if isinstance(value, int | float) else math.nan
