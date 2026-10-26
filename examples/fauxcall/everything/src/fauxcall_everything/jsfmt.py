"""JavaScript value semantics the real server (Node.js) applies to what it writes.

The everything server builds its texts with template literals and ``JSON.stringify``: numbers are
spelled the way ``Number.prototype.toString`` spells them (``1e+21``, ``0.30000000000000004``,
``2`` for ``2.0``), strings are parsed with ``Number()``, and values interpolate with
``String()``. These helpers reproduce exactly that.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from fauxcall.jsonfmt import JsonFloat

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

MAX_SAFE_INTEGER: Final = 2**53 - 1
"""Integers up to this size print the same in Python and JavaScript."""
_EXPONENT_LIMIT: Final = 21
"""``Number.prototype.toString`` switches to exponents from 1e21 on."""
_SMALL_LIMIT: Final = -6
"""… and below 1e-6."""
JS_WHITESPACE: Final = "\t\n\v\f\r                  　﻿"
_DECIMAL: Final = re.compile(r"[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)")
_PREFIXED: Final = {"0x": 16, "0o": 8, "0b": 2}


class Undefined:
    """JavaScript's ``undefined``: a value that is absent (``JSON.stringify`` drops it)."""

    def __repr__(self) -> str:
        return "undefined"


UNDEFINED: Final = Undefined()


def to_float(value: JsonValue) -> float:
    """A JSON number as the double JavaScript holds for it (``int`` beyond 2**53 rounds)."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int):
        try:
            return float(value)
        except OverflowError:
            return math.inf if value > 0 else -math.inf
    if isinstance(value, float):
        return float(value)
    raise TypeError(f"not a number: {value!r}")


def number_text(value: float) -> str:
    """``String(value)`` for a JavaScript number (``Number.prototype.toString()``)."""
    if math.isnan(value):
        return "NaN"
    if value == 0:
        return "0"  # -0 too
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    sign = "-" if value < 0 else ""
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = list(digit_tuple)
    assert isinstance(exponent, int)  # noqa: S101 - finite numbers only
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    text = "".join(str(digit) for digit in digits)
    count = len(digits)
    point = count + exponent  # value = 0.<digits> * 10**point
    if count <= point <= _EXPONENT_LIMIT:
        return sign + text + "0" * (point - count)
    if 0 < point <= _EXPONENT_LIMIT:
        return sign + text[:point] + "." + text[point:]
    if _SMALL_LIMIT < point <= 0:
        return sign + "0." + "0" * -point + text
    power = point - 1
    mantissa = text[0] + ("." + text[1:] if count > 1 else "")
    return f"{sign}{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


def json_number(value: float) -> int | float:
    """A number to put in a JSON answer so it is written as JavaScript writes it."""
    if math.isfinite(value) and value.is_integer() and abs(value) <= MAX_SAFE_INTEGER:
        return int(value)
    return JsonFloat(number_text(value))


def to_number(text: str) -> float:
    """``Number(text)``: JavaScript's string-to-number conversion (``NaN`` when not a number)."""
    stripped = text.strip(JS_WHITESPACE)
    if not stripped:
        return 0.0
    lowered = stripped.lower()
    base = _PREFIXED.get(lowered[:2])
    if base is not None:
        try:
            return float(int(stripped[2:], base)) if stripped[2:].isalnum() else math.nan
        except ValueError:
            return math.nan
    if stripped in {"Infinity", "+Infinity"}:
        return math.inf
    if stripped == "-Infinity":
        return -math.inf
    if not _DECIMAL.fullmatch(stripped):
        return math.nan
    return float(stripped)


def is_positive_integer(value: float) -> bool:
    """``Number.isFinite(n) && Number.isInteger(n) && n >= 1``."""
    return math.isfinite(value) and value.is_integer() and value >= 1


def js_string(value: JsonValue | Undefined) -> str:
    """``String(value)``, as a template literal interpolates it."""
    if isinstance(value, Undefined):
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return number_text(to_float(value))
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None else js_string(item) for item in value)
    return "[object Object]"


def stringify(value: JsonValue | Undefined, indent: int = 2) -> str:
    """``JSON.stringify(value, null, indent)``, numbers spelled as JavaScript spells them."""
    return _encode(value, indent, 0)


def _encode(value: JsonValue | Undefined, indent: int, depth: int) -> str:
    if value is None or isinstance(value, Undefined):
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        number = to_float(value)
        return number_text(number) if math.isfinite(number) else "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    inner = "\n" + " " * (indent * (depth + 1)) if indent else ""
    outer = "\n" + " " * (indent * depth) if indent else ""
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [inner + _encode(item, indent, depth + 1) for item in value]
        return "[" + ",".join(items) + outer + "]"
    colon = ": " if indent else ":"
    members = [
        inner + json.dumps(key, ensure_ascii=False) + colon + _encode(item, indent, depth + 1)
        for key, item in value.items()
        if not isinstance(item, Undefined)
    ]
    return "{" + ",".join(members) + outer + "}" if members else "{}"
