"""Byte-faithful JSON handling.

Real APIs differ in how they *spell* JSON: separators, unicode escaping, how floats are written
(``105.50`` vs ``105.5``). Agents only care about values, but a fake that re-spells everything is
easy to spot and harder to diff against recordings. This module:

* parses JSON while remembering each float's original spelling (:class:`JsonFloat`),
* infers the formatting style of a real API from raw response bodies (:func:`infer_style`),
* serializes values back in that style (:func:`dumps`).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fauxcall.types import JsonValue


class JsonFloat(float):
    """A float that remembers how it was written in the source document."""

    __slots__ = ("lexeme",)

    lexeme: str

    def __new__(cls, lexeme: str | float) -> Self:
        """Create a float that remembers ``lexeme`` (or the shortest spelling of a float)."""
        obj = super().__new__(cls, lexeme)
        obj.lexeme = lexeme if isinstance(lexeme, str) else float.__repr__(float(lexeme))
        return obj

    def __getnewargs__(self) -> tuple[str]:  # type: ignore[override]
        return (self.lexeme,)


def _reject_constant(name: str) -> float:
    raise ValueError(f"JSON constant {name!r} is not valid JSON")


def loads(text: str | bytes) -> JsonValue:
    """Parse JSON, preserving key order and each float's spelling."""
    value: JsonValue = json.loads(text, parse_float=JsonFloat, parse_constant=_reject_constant)
    return value


def plain(value: JsonValue) -> JsonValue:
    """Return a copy of ``value`` with :class:`JsonFloat` instances turned into plain floats."""
    if isinstance(value, JsonFloat):
        return float(value)
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


class JsonStyle(BaseModel):
    """How a particular API spells its JSON."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_separator: str = ","
    key_separator: str = ":"
    indent: int | None = None
    ensure_ascii: bool = False
    escape_slash: bool = False
    escape_html: bool = False
    upper_hex: bool = False
    trailing_newline: bool = False


COMPACT: Final = JsonStyle()
"""``{"a":1,"b":[1,2]}`` — the most common style for HTTP APIs."""

_SIMPLE_ESCAPES: Final = {
    '"': '\\"',
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}
_HTML_CHARS: Final = frozenset("<>&'")
_FIRST_PRINTABLE: Final = 0x20
_MAX_BMP: Final = 0xFFFF


def _needs_escape_pattern(style: JsonStyle) -> re.Pattern[str]:
    chars = r'"\\\x00-\x1f'
    if style.escape_slash:
        chars += "/"
    if style.escape_html:
        chars += "<>&'"
    if style.ensure_ascii:
        chars += r"\x7f-\U0010ffff"
    return re.compile(f"[{chars}]")


def _unicode_escape(code_point: int, *, upper: bool) -> str:
    def one(unit: int) -> str:
        digits = f"{unit:04X}" if upper else f"{unit:04x}"
        return "\\u" + digits

    if code_point > _MAX_BMP:
        code_point -= 0x10000
        high = 0xD800 | (code_point >> 10)
        low = 0xDC00 | (code_point & 0x3FF)
        return one(high) + one(low)
    return one(code_point)


class _Encoder:
    def __init__(self, style: JsonStyle) -> None:
        self.style = style
        self.pattern = _needs_escape_pattern(style)

    def _escape_char(self, match: re.Match[str]) -> str:
        char = match.group(0)
        if char in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[char]
        if char == "/":
            return "\\/"
        if char in _HTML_CHARS or ord(char) < _FIRST_PRINTABLE or self.style.ensure_ascii:
            return _unicode_escape(ord(char), upper=self.style.upper_hex)
        return char  # pragma: no cover - the pattern only matches characters handled above

    def string(self, value: str) -> str:
        return '"' + self.pattern.sub(self._escape_char, value) + '"'

    def number(self, value: float) -> str:
        if isinstance(value, JsonFloat):
            return value.lexeme
        if isinstance(value, int):
            return str(value)
        if not math.isfinite(value):
            raise ValueError(f"{value!r} cannot be represented in JSON")
        return float.__repr__(value)

    def encode(self, value: JsonValue, depth: int = 0) -> str:
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, str):
            return self.string(value)
        if isinstance(value, (int, float)):
            return self.number(value)
        if isinstance(value, list):
            if not value:
                return "[]"
            items = [self.encode(item, depth + 1) for item in value]
            return self._join("[", "]", items, depth)
        if isinstance(value, dict):
            if not value:
                return "{}"
            items = [
                self.string(key) + self.style.key_separator + self.encode(item, depth + 1)
                for key, item in value.items()
            ]
            return self._join("{", "}", items, depth)
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    def _join(self, opener: str, closer: str, items: list[str], depth: int) -> str:
        indent = self.style.indent
        if indent is None:
            return opener + self.style.item_separator.join(items) + closer
        inner = "\n" + " " * (indent * (depth + 1))
        outer = "\n" + " " * (indent * depth)
        separator = self.style.item_separator.rstrip() + inner
        return opener + inner + separator.join(items) + outer + closer


def dumps(value: JsonValue, style: JsonStyle = COMPACT) -> str:
    """Serialize ``value`` in ``style``, reproducing original float spellings."""
    text = _Encoder(style).encode(value)
    return text + "\n" if style.trailing_newline else text


def canonical_dumps(value: JsonValue) -> str:
    """A stable, sorted, ASCII-only spelling used for hashing and cache keys."""
    return json.dumps(plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# Style inference
# ---------------------------------------------------------------------------

_UNICODE_ESCAPE: Final = re.compile(r"\\u([0-9a-fA-F]{4})")


def _structural_chars(text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(index, char)`` for characters outside string literals."""
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        yield index, char


@dataclass(slots=True)
class _StyleEvidence:
    key_sep: str | None = None
    item_sep: str | None = None
    indent: int | None = None
    ascii_escapes: bool = False
    raw_non_ascii: bool = False
    escape_slash: bool = False
    escape_html: bool = False
    upper_hex: bool | None = None
    trailing_newline: bool = False

    def observe(self, text: str) -> None:
        self._observe_structure(text)
        self._observe_escapes(text)
        if text.endswith("\n"):
            self.trailing_newline = True

    def _observe_structure(self, text: str) -> None:
        for index, char in _structural_chars(text):
            following = text[index + 1 : index + 2]
            if char == ":" and self.key_sep is None:
                self.key_sep = ": " if following == " " else ":"
            elif char == "," and self.item_sep is None:
                self.item_sep = ", " if following == " " else ","
            elif char in "[{" and self.indent is None and following == "\n":
                rest = text[index + 2 :]
                self.indent = len(rest) - len(rest.lstrip(" "))
            if self.key_sep is not None and self.item_sep is not None and self.indent is not None:
                return

    def _observe_escapes(self, text: str) -> None:
        if "\\/" in text:
            self.escape_slash = True
        for match in _UNICODE_ESCAPE.finditer(text):
            digits = match.group(1)
            code_point = int(digits, 16)
            if code_point >= 0x80:  # noqa: PLR2004 - first non-ASCII code point
                self.ascii_escapes = True
            if chr(code_point) in _HTML_CHARS:
                self.escape_html = True
            if any(c.isalpha() for c in digits) and self.upper_hex is None:
                self.upper_hex = digits.upper() == digits
        if any(ord(c) >= 0x80 for c in text):  # noqa: PLR2004
            self.raw_non_ascii = True


def infer_style(raw_texts: Iterable[str]) -> JsonStyle:
    """Infer the JSON style an API uses from raw response bodies.

    Evidence is combined across samples; anything not observed falls back to the compact default.
    """
    evidence = _StyleEvidence()
    for text in raw_texts:
        evidence.observe(text)
    indent = evidence.indent or None
    key_sep = evidence.key_sep or (": " if indent else ":")
    item_sep = evidence.item_sep or ","
    return JsonStyle(
        item_separator=item_sep,
        key_separator=key_sep,
        indent=indent,
        ensure_ascii=evidence.ascii_escapes and not evidence.raw_non_ascii,
        escape_slash=evidence.escape_slash,
        escape_html=evidence.escape_html,
        upper_hex=bool(evidence.upper_hex),
        trailing_newline=evidence.trailing_newline,
    )
