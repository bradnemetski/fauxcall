"""JavaScript value semantics the real (Node.js) server's answers depend on.

The server builds its texts with template literals and ``JSON.stringify``, parses resource ids
with ``Number()``, and does its arithmetic in IEEE doubles: ``get-sum`` answers ``0.1 + 0.2`` with
``0.30000000000000004`` and ``1e21`` as ``1e+21``. These helpers spell values exactly as Node does.
"""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

_DECIMAL: Final = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_RADIX: Final = {"0x": 16, "0o": 8, "0b": 2}
JS_WHITESPACE: Final = (
    " \t\n\r\v\f\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
MAX_SAFE_INTEGER: Final = 2**53 - 1


def is_number(value: object) -> bool:
    """Whether a JSON value is a number (JSON booleans are not)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def to_double(value: float) -> float:
    """A JSON number as JavaScript holds it: a double (huge integers round, overflow is ±∞)."""
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def number_to_string(value: float) -> str:  # noqa: PLR0911 - one return per case
    """``Number.prototype.toString()``: ``3``, ``0.30000000000000004``, ``1e+21``, ``1e-7``."""
    number = to_double(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    if number < 0:
        return "-" + number_to_string(-number)
    mantissa, _, exponent = repr(number).partition("e")
    whole, _, fraction = mantissa.partition(".")
    every = whole + fraction
    digits = every.lstrip("0")
    leading = len(every) - len(digits)
    # value = 0.<digits> x 10^point: the position of the decimal point among the digits
    point = len(whole) + (int(exponent) if exponent else 0) - leading
    digits = digits.rstrip("0")
    count = len(digits)
    if count <= point <= 21:  # noqa: PLR2004 - JavaScript's threshold for exponent notation
        return digits + "0" * (point - count)
    if 0 < point <= 21:  # noqa: PLR2004
        return digits[:point] + "." + digits[point:]
    if -6 < point <= 0:  # noqa: PLR2004
        return "0." + "0" * (-point) + digits
    exponent_value = point - 1
    sign = "+" if exponent_value >= 0 else "-"
    head = digits[0] + ("." + digits[1:] if count > 1 else "")
    return f"{head}e{sign}{abs(exponent_value)}"


def parse_number(text: str) -> float:  # noqa: PLR0911 - one return per case
    """``Number(text)``: decimal, ``0x``/``0o``/``0b`` literals, ``Infinity``; else ``NaN``."""
    trimmed = text.strip(JS_WHITESPACE)
    if not trimmed:
        return 0.0
    if trimmed in {"Infinity", "+Infinity"}:
        return math.inf
    if trimmed == "-Infinity":
        return -math.inf
    radix = _RADIX.get(trimmed[:2].lower())
    if radix is not None:
        try:
            return float(int(trimmed[2:], radix)) if trimmed[2:].isalnum() else math.nan
        except ValueError:
            return math.nan
    if _DECIMAL.match(trimmed):
        return to_double(float(trimmed))
    return math.nan


def is_integer(number: float) -> bool:
    """``Number.isInteger``."""
    return math.isfinite(number) and number == math.floor(number)


def json_number(number: float) -> int | float:
    """A double as JSON.stringify spells it: integral values without a fraction."""
    if is_integer(number) and abs(number) <= MAX_SAFE_INTEGER:
        return int(number)
    return number


def template(value: JsonValue | None) -> str:
    """A value interpolated into a template literal (``${value}``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if is_number(value):
        return number_to_string(value)  # type: ignore[arg-type]
    if isinstance(value, list):
        return ",".join("" if item is None else template(item) for item in value)
    return "[object Object]"


def truthy(value: JsonValue | None) -> bool:
    """JavaScript truthiness: ``""``, ``0``, ``null`` and ``undefined`` are false; ``{}`` isn't."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value != ""
    if is_number(value):
        return value != 0 and not math.isnan(value)  # type: ignore[arg-type]
    return True


def stringify(value: JsonValue, indent: int | None = None, _level: int = 0) -> str:  # noqa: PLR0911
    """``JSON.stringify(value, null, indent)``: Node's spelling of numbers, no ASCII escaping."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        number = to_double(value)
        return number_to_string(number) if math.isfinite(number) else "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    pad = "" if indent is None else "\n" + " " * (indent * (_level + 1))
    end = "" if indent is None else "\n" + " " * (indent * _level)
    colon = ":" if indent is None else ": "
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [stringify(item, indent, _level + 1) for item in value]
        return "[" + pad + ("," + pad).join(items) + end + "]"
    if not value:
        return "{}"
    members = [
        json.dumps(key, ensure_ascii=False) + colon + stringify(item, indent, _level + 1)
        for key, item in value.items()
    ]
    return "{" + pad + ("," + pad).join(members) + end + "}"


def type_name(value: object, *, present: bool = True) -> str:  # noqa: PLR0911 - one per type
    """Zod's name for a received value's type (``undefined`` when absent)."""
    if not present:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        number = to_double(value)  # zod names a non-finite number by its value
        return number_to_string(number) if math.isinf(number) else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"
