"""JavaScript value semantics the real server's text follows (it is a Node.js process).

Its answers spell numbers the way JavaScript does (``1e+21``, ``5e-7``, ``10000000000000000``,
``-0`` as ``0``), convert strings with ``Number()``, test truthiness, interpolate values into
template strings and pretty-print with ``JSON.stringify(value, null, 2)``. Python spells all of
these differently, so the fake does it here.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

MAX_SAFE_INTEGER: Final = 2**53
_WHITESPACE: Final = "".join(
    chr(code) for code in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B))
) + "".join(chr(code) for code in (0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF))
"""What ``Number()`` trims: JavaScript's white space and line terminators."""
_DECIMAL: Final = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_RADIX: Final = re.compile(r"0([xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)")
_INFINITY: Final = re.compile(r"[+-]?Infinity")


def to_double(value: float) -> float:
    """A JSON number as JavaScript holds it: a double (big integers lose precision)."""
    return float(value)


def number_text(value: float) -> str:  # noqa: PLR0911 - one return per ECMAScript case
    """``String(n)`` / `` `${n}` `` for a JavaScript number."""
    number = to_double(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    digits, exponent = _shortest(abs(number))
    k = len(digits)
    n = exponent
    if k <= n <= 21:  # noqa: PLR2004 - ECMAScript Number::toString thresholds
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:  # noqa: PLR2004
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:  # noqa: PLR2004
        return sign + "0." + "0" * (-n) + digits
    shown = n - 1
    mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
    return f"{sign}{mantissa}e{'+' if shown >= 0 else '-'}{abs(shown)}"


def _shortest(number: float) -> tuple[str, int]:
    """The shortest round-trip digits of a positive double and n, where value = 0.d1d2… x 10^n."""
    sign, digit_tuple, exponent = Decimal(repr(number)).as_tuple()
    del sign
    digits = "".join(str(d) for d in digit_tuple).lstrip("0") or "0"
    stripped = digits.rstrip("0")
    trailing = len(digits) - len(stripped)
    assert isinstance(exponent, int)  # noqa: S101 - finite numbers only
    # value = digits x 10^exponent = stripped x 10^(exponent + trailing)
    return stripped, len(stripped) + exponent + trailing


def plain(value: JsonValue) -> JsonValue:
    """A value with JavaScript's number model: integral doubles become integers.

    ``JSON.stringify(2.0)`` is ``2``; Python would write ``2.0``.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INTEGER else float(value)
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and abs(value) <= MAX_SAFE_INTEGER:
            return int(value)
        return value
    if isinstance(value, list):
        return [plain(item) for item in value]
    return {key: plain(item) for key, item in value.items()}


def stringify(value: JsonValue, *, indent: int | None = None) -> str:
    """``JSON.stringify(value, null, indent)``: JavaScript's spacing and number spelling."""
    if indent is None:
        return _compact(value)
    return _pretty(value, indent, 0)


def _scalar(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        number = to_double(value) if isinstance(value, float) else value
        if isinstance(number, float) and not math.isfinite(number):
            return "null"
        return number_text(number)
    return json.dumps(value, ensure_ascii=False)


def _compact(value: JsonValue) -> str:
    if isinstance(value, list):
        return "[" + ",".join(_compact(item) for item in value) + "]"
    if isinstance(value, dict):
        members = (
            json.dumps(key, ensure_ascii=False) + ":" + _compact(v) for key, v in value.items()
        )
        return "{" + ",".join(members) + "}"
    return _scalar(value)


def _pretty(value: JsonValue, indent: int, depth: int) -> str:
    inner = " " * (indent * (depth + 1))
    outer = " " * (indent * depth)
    if isinstance(value, list):
        if not value:
            return "[]"
        items = (inner + _pretty(item, indent, depth + 1) for item in value)
        return "[\n" + ",\n".join(items) + "\n" + outer + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        members = (
            inner + json.dumps(key, ensure_ascii=False) + ": " + _pretty(item, indent, depth + 1)
            for key, item in value.items()
        )
        return "{\n" + ",\n".join(members) + "\n" + outer + "}"
    return _scalar(value)


def template(value: JsonValue) -> str:
    """`` `${value}` ``: how a JavaScript template string shows a value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return number_text(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None else template(item) for item in value)
    return "[object Object]"


def truthy(value: JsonValue) -> bool:
    """JavaScript truthiness (objects and arrays are always true)."""
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, int | float):
        return value != 0 and not math.isnan(value)
    if isinstance(value, str):
        return value != ""
    return True


def number_of(text: str) -> float:
    """``Number(text)`` for a string: trimmed, ``""`` is 0, hex/octal/binary, else NaN."""
    stripped = text.strip(_WHITESPACE)
    if not stripped:
        return 0.0
    if _INFINITY.fullmatch(stripped):
        return -math.inf if stripped.startswith("-") else math.inf
    radix = _RADIX.fullmatch(stripped)
    if radix:
        base = {"x": 16, "o": 8, "b": 2}[radix.group(1)[0].lower()]
        return float(int(radix.group(1)[1:], base))
    if _DECIMAL.fullmatch(stripped):
        return float(stripped)
    return math.nan


def trim(text: str) -> str:
    """``String.prototype.trim()``: JavaScript's white space and line terminators."""
    return text.strip(_WHITESPACE)


def is_positive_integer(number: float) -> bool:
    """``Number.isFinite(n) && Number.isInteger(n) && n >= 1``."""
    return math.isfinite(number) and float(number).is_integer() and number >= 1


def type_name(value: JsonValue) -> str:
    """The name zod gives a received value (``received null``, ``received array`` …)."""
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
