"""JavaScript number semantics the real server (Node.js) shows in its text answers.

``String(x)`` / template literals format numbers with ``Number.prototype.toString`` (``1e+21``,
``1.5e-7``, ``0.30000000000000004``, ``Infinity``), and ``Number(text)`` parses resource ids
(``"01"`` -> 1, ``"1e2"`` -> 100, ``"0x10"`` -> 16, ``""`` -> 0).
"""

from __future__ import annotations

import math
import re
from typing import Final

_EXPONENT_MIN: Final = -6
_EXPONENT_MAX: Final = 21
_DECIMAL: Final = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
_DIGITS: Final = {16: "0123456789abcdef", 8: "01234567", 2: "01"}
_RADIX: Final = {"0x": 16, "0o": 8, "0b": 2}
_WHITESPACE: Final = "".join(
    map(chr, (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0xFEFF, 0x1680, 0x2028, 0x2029))
) + "".join(map(chr, (*range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000)))


def as_double(value: float) -> float:
    """The IEEE double JavaScript holds for a JSON number (integers beyond 2**53 round)."""
    try:
        return float(value)
    except OverflowError:  # an integer literal beyond the double range is Infinity in JS
        return math.inf if value > 0 else -math.inf


def js_string(value: float) -> str:
    """``String(value)`` for a JavaScript number."""
    number = as_double(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    digits, point = _shortest_digits(abs(number))
    return sign + _layout(digits, point)


def _shortest_digits(number: float) -> tuple[str, int]:
    """The shortest round-trip digits ``d`` and ``n`` with ``number == 0.d * 10**n``."""
    mantissa, _, exponent = f"{number!r}".partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    leading_zeros = len(whole + fraction) - len((whole + fraction).lstrip("0"))
    point = len(whole) - leading_zeros + (int(exponent) if exponent else 0)
    return digits.rstrip("0"), point


def _layout(digits: str, point: int) -> str:
    """ECMAScript Number::toString layout of digits ``d`` (k of them) and decimal point ``n``."""
    k = len(digits)
    if k <= point <= _EXPONENT_MAX:
        return digits + "0" * (point - k)
    if 0 < point <= _EXPONENT_MAX:
        return f"{digits[:point]}.{digits[point:]}"
    if _EXPONENT_MIN < point <= 0:
        return "0." + "0" * -point + digits
    exponent = point - 1
    sign = "+" if exponent >= 0 else "-"
    head = digits[0] if k == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{head}e{sign}{abs(exponent)}"


def js_number(text: str) -> float:  # noqa: PLR0911 - one return per syntax
    """``Number(text)`` for a string: NaN when it isn't a number."""
    stripped = text.strip(_WHITESPACE)
    if not stripped:
        return 0.0
    lowered = stripped.lower()
    radix = _RADIX.get(lowered[:2])
    if radix is not None:
        digits = lowered[2:]
        if not digits or any(d not in _DIGITS[radix] for d in digits):
            return math.nan
        return float(int(digits, radix))
    if stripped in {"Infinity", "+Infinity"}:
        return math.inf
    if stripped == "-Infinity":
        return -math.inf
    if _DECIMAL.fullmatch(stripped) is None:
        return math.nan
    try:
        return float(stripped)
    except OverflowError:  # pragma: no cover - float() saturates rather than raising
        return math.inf if not stripped.startswith("-") else -math.inf


def is_positive_integer(value: float) -> bool:
    """``Number.isFinite(v) && Number.isInteger(v) && v >= 1`` for a JavaScript number."""
    number = as_double(value)
    return math.isfinite(number) and number.is_integer() and number >= 1
