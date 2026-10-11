"""How the real API (ASP.NET Core + System.Text.Json, web defaults) binds and echoes JSON bodies.

Every rule here comes from recordings (``data/plans/probes-2.json`` and ``probes-3.json``):

- The body is read token by token; the **first** problem wins and is reported as
  ``"<message> Path: <path> | LineNumber: <line> | BytePositionInLine: <byte>."`` under the key
  ``<path>``. Lines and byte positions are zero-based and count UTF-8 bytes.
- Syntax errors point at the offending byte; conversion errors point just past the value token
  (just past ``{``/``[`` for containers).
- Property names match case-insensitively, duplicates keep the last value, unknown properties are
  skipped (but must still be valid JSON), missing properties keep the .NET defaults.
- Numbers may arrive as strings (``"5"``, ``"+7"``); ``1.0``, ``1e2`` and out-of-range values fail.
- ``DateTime`` values are ISO 8601; fractions are truncated to 7 digits; offsets are converted to
  the server's local time (UTC) and written with ``+00:00``; ``Z`` stays ``Z``; no zone stays
  unqualified.
- Responses use the relaxed encoder: quotes, backslashes, control/format-less categories (control,
  private use, unassigned, line/paragraph separators, surrogates) and characters outside the BMP
  are escaped (upper-case hex); ``é€<>&'+`` and U+00AD are written as they are.
- Containers nest at most 32 deep (the root object counts).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

FieldType = Literal["int", "string", "bool", "datetime", "uri"]

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1
DATETIME_DEFAULT: Final = "0001-01-01T00:00:00"

_CLR_TYPES: Final[dict[FieldType, str]] = {
    "int": "System.Int32",
    "string": "System.String",
    "bool": "System.Boolean",
    "datetime": "System.DateTime",
    "uri": "System.Uri",
}
_DEFAULTS: Final[dict[FieldType, object]] = {
    "int": 0,
    "string": None,
    "bool": False,
    "datetime": DATETIME_DEFAULT,
    "uri": None,
}
_WS: Final = b" \t\r\n"
_ESCAPABLE: Final = b'"\\/bfnrtu'
_HEX: Final = b"0123456789abcdefABCDEF"
_DIGITS: Final = b"0123456789"
MAX_DEPTH: Final = 32
"""JsonSerializerOptions.MaxDepth of the real API (recorded: 'maximum configured depth of 32')."""
_ESCAPED_CATEGORIES: Final = frozenset({"Cc", "Cs", "Co", "Cn", "Zl", "Zp"})
_LITERALS: Final[dict[int, bytes]] = {ord("t"): b"true", ord("f"): b"false", ord("n"): b"null"}
_INT_STRING: Final = re.compile(r"[+-]?[0-9]+")
_INT_NUMBER: Final = re.compile(r"-?[0-9]+")
_ISO: Final = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,16}))?)?(Z|[+-]\d{2}:\d{2})?)?"
)

EMPTY_BODY: Final = "A non-empty request body is required."
_NO_TOKENS: Final = (
    "The input does not contain any JSON tokens. Expected the input to start with a valid JSON"
    " token, when isFinalBlock is true."
)
_DEPTH: Final = (
    "Expected depth to be zero at the end of the JSON payload. There is an open JSON object or"
    " array that should be closed."
)
_TRAILING: Final = (
    "The JSON {0} contains a trailing comma at the end which is not supported in this mode."
    " Change the reader options."
)


@dataclass(frozen=True)
class Dto:
    """A model class of the real API: its CLR name and its properties in declaration order."""

    clr_name: str
    fields: tuple[tuple[str, FieldType], ...]


class BindingError(Exception):
    """A body the real API refuses: ``errors`` is the ``errors`` member of its 400 response."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key
        self.message = message


class _ReaderError(Exception):
    def __init__(self, message: str, path: str, index: int) -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        self.index = index


def _char(data: bytes, index: int) -> str:
    """The character at ``index`` as the real API quotes it in messages."""
    byte = data[index]
    if byte < 0x80:  # noqa: PLR2004
        return chr(byte)
    end = index + 1
    while end < len(data) and data[end] & 0xC0 == 0x80:  # noqa: PLR2004
        end += 1
    return data[index:end].decode("utf-8", errors="replace")


class _Reader:
    """A byte-level JSON reader that fails exactly where (and how) Utf8JsonReader does."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.n = len(data)

    def ws(self, i: int) -> int:
        while i < self.n and self.data[i] in _WS:
            i += 1
        return i

    def string(self, i: int, path: str) -> tuple[str, int]:
        """The string starting at the quote at ``i``; returns (value, index after it)."""
        data = self.data
        j = i + 1
        while j < self.n:
            byte = data[j]
            if byte == ord('"'):
                text = data[i : j + 1].decode("utf-8", errors="replace")
                return json.loads(text), j + 1
            if byte == ord("\\"):
                if j + 1 >= self.n:
                    break
                j = self._escape(j + 1, path)
                continue
            if byte < 0x20:  # noqa: PLR2004
                raise _ReaderError(
                    f"'0x{byte:02X}' is invalid within a JSON string. The string should be"
                    " correctly escaped.",
                    path,
                    j,
                )
            j += 1
        raise _ReaderError("Expected end of string, but instead reached end of data.", path, self.n)

    def _escape(self, j: int, path: str) -> int:
        """Check the escape whose letter is at ``j``; returns the index after it."""
        data = self.data
        if data[j] not in _ESCAPABLE:
            raise _ReaderError(
                f"'{_char(data, j)}' is an invalid escapable character within a JSON"
                " string. The string should be correctly escaped.",
                path,
                j,
            )
        if data[j] != ord("u"):
            return j + 1
        for k in range(j + 1, min(j + 5, self.n)):
            if data[k] not in _HEX:
                raise _ReaderError(
                    f"'{_char(data, k)}' is not a hex digit following '\\u' within a"
                    " JSON string. The string should be correctly escaped.",
                    path,
                    k,
                )
        return j + 5

    def number(self, i: int, path: str, *, nested: bool) -> tuple[str, int]:
        """The number starting at ``i``; returns (text, index after it)."""
        data = self.data
        j = self._integer(i, path)
        if j < self.n and data[j] == ord("."):
            j = self._digits(j + 1, path, "a decimal point ('.')")
        if j < self.n and data[j] in b"eE":
            j += 1
            if j < self.n and data[j] in b"+-":
                j += 1
            j = self._digits(j, path, "the exponent character ('e' or 'E')")
        if j >= self.n:
            if nested:  # the body ended inside an object: the reader still wanted more digits
                raise _ReaderError(
                    f"'{_char(data, j - 1)}' is an invalid end of a number. Expected a delimiter.",
                    path,
                    self.n,
                )
        elif data[j] not in _WS and data[j] not in b",}]":
            raise _ReaderError(
                f"'{_char(data, j)}' is an invalid end of a number. Expected a delimiter.",
                path,
                j,
            )
        return data[i:j].decode("ascii"), j

    def _integer(self, i: int, path: str) -> int:
        """The sign and integer part of a number; returns the index after them."""
        data = self.data
        j = i
        if data[j] == ord("-"):
            j += 1
            if j >= self.n:
                raise _ReaderError(
                    "Expected a digit ('0'-'9'), but instead reached end of data.", path, self.n
                )
            if data[j] not in _DIGITS:
                raise _ReaderError(
                    f"'{_char(data, j)}' is invalid within a number, immediately after a sign"
                    " character ('+' or '-'). Expected a digit ('0'-'9').",
                    path,
                    j,
                )
        if data[j] == ord("0"):
            j += 1
            if j < self.n and data[j] in _DIGITS:
                raise _ReaderError(f"Invalid leading zero before '{_char(data, j)}'.", path, j)
            return j
        while j < self.n and data[j] in _DIGITS:
            j += 1
        return j

    def _digits(self, j: int, path: str, after: str) -> int:
        if j >= self.n:
            raise _ReaderError(
                "Expected a digit ('0'-'9'), but instead reached end of data.", path, self.n
            )
        if self.data[j] not in _DIGITS:
            raise _ReaderError(
                f"'{_char(self.data, j)}' is invalid within a number, immediately after {after}."
                " Expected a digit ('0'-'9').",
                path,
                j,
            )
        while j < self.n and self.data[j] in _DIGITS:
            j += 1
        return j

    def literal(self, i: int, path: str) -> tuple[str, int]:
        expected = _LITERALS[self.data[i]]
        for k, byte in enumerate(expected):
            if i + k >= self.n or self.data[i + k] != byte:
                # quoted from the literal's start to the end of the body ('tru\r\n}', recorded)
                text = self.data[i:].decode("utf-8", errors="replace")
                raise _ReaderError(
                    f"'{text}' is an invalid JSON literal. Expected the literal"
                    f" '{expected.decode()}'.",
                    path,
                    min(i + k, self.n),
                )
        return expected.decode(), i + len(expected)

    def scalar(self, i: int, path: str, *, nested: bool) -> tuple[str, object, int]:
        """A non-container value at ``i``: (kind, value, index after it)."""
        byte = self.data[i]
        if byte == ord('"'):
            text, end = self.string(i, path)
            return "string", text, end
        if byte == ord("-") or byte in _DIGITS:
            number, end = self.number(i, path, nested=nested)
            return "number", number, end
        if byte in _LITERALS:
            word, end = self.literal(i, path)
            return word, None, end
        raise _ReaderError(f"'{_char(self.data, i)}' is an invalid start of a value.", path, i)

    def skip(self, i: int, path: str, depth: int) -> int:
        """Validate and skip any value (unknown properties); returns the index after it.

        ``depth`` is the number of objects and arrays already open around the value.
        """
        byte = self.data[i]
        if byte == ord("{"):
            return self.container(i, path, "object", depth)
        if byte == ord("["):
            return self.container(i, path, "array", depth)
        return self.scalar(i, path, nested=True)[2]

    def container(self, i: int, path: str, kind: Literal["object", "array"], depth: int) -> int:
        if depth >= MAX_DEPTH:
            raise _ReaderError(
                f"The maximum configured depth of {MAX_DEPTH} has been exceeded."
                f" Cannot read next JSON {kind}.",
                path,
                i,
            )
        close = ord("}") if kind == "object" else ord("]")
        i = self.ws(i + 1)
        if i >= self.n:
            raise _ReaderError(_DEPTH, path, self.n)
        if self.data[i] == close:
            return i + 1
        while True:
            if kind == "object":
                i = self.property_name(i, path)
            i = self.ws(self.skip(i, path, depth + 1))
            if i >= self.n:
                raise _ReaderError(_DEPTH, path, self.n)
            i, done = self.separator(i, path, close, kind)
            if done:
                return i

    def property_name(self, i: int, path: str) -> int:
        """Read ``"name":`` at ``i``; returns the index of the value."""
        if self.data[i] != ord('"'):
            raise _ReaderError(
                f"'{_char(self.data, i)}' is an invalid start of a property name. Expected a '\"'.",
                path,
                i,
            )
        _, i = self.string(i, path)
        return self.colon(i, path)

    def colon(self, i: int, path: str) -> int:
        i = self.ws(i)
        if i >= self.n:
            raise _ReaderError("Expected a value, but instead reached end of data.", path, self.n)
        if self.data[i] != ord(":"):
            raise _ReaderError(
                f"'{_char(self.data, i)}' is invalid after a property name. Expected a ':'.",
                path,
                i,
            )
        i = self.ws(i + 1)
        if i >= self.n:
            raise _ReaderError("Expected a value, but instead reached end of data.", path, self.n)
        return i

    def separator(
        self, i: int, path: str, close: int, kind: Literal["object", "array"]
    ) -> tuple[int, bool]:
        """After a value: ``,`` (returns the next item's index) or the closing bracket."""
        byte = self.data[i]
        if byte == close:
            return i + 1, True
        if byte != ord(","):
            raise _ReaderError(
                f"'{_char(self.data, i)}' is invalid after a value."
                " Expected either ',', '}', or ']'.",
                path,
                i,
            )
        comma = i
        i = self.ws(i + 1)
        if i >= self.n:
            raise _ReaderError(
                "Expected start of a property name or value, but instead reached end of data.",
                path,
                comma,
            )
        if self.data[i] == close:
            raise _ReaderError(_TRAILING.format(kind), path, i)
        return i, False


def _int(text: str, pattern: re.Pattern[str]) -> int | None:
    if not pattern.fullmatch(text):
        return None
    value = int(text)
    return value if INT32_MIN <= value <= INT32_MAX else None


def parse_datetime(text: str) -> str | None:
    """A ``DateTime`` as the real API echoes it, or ``None`` when it can't be parsed."""
    match = _ISO.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        moment = datetime(  # noqa: DTZ001 - DateTime values are zone-less until written
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        )
    except ValueError:
        return None
    digits = (fraction or "")[:7].rstrip("0")
    suffix = ""
    if zone == "Z":
        suffix = "Z"
    elif zone:
        sign = 1 if zone[0] == "+" else -1
        offset = timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
        try:
            moment -= sign * offset
        except OverflowError:
            return None
        suffix = "+00:00"
    spelled = (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )
    return spelled + (f".{digits}" if digits else "") + suffix


def _convert(field: FieldType, kind: str, value: object) -> tuple[bool, object]:
    """Convert one scalar token to a property's type: (ok, value)."""
    result: tuple[bool, object] = (False, None)
    match field, kind:
        case "int", "number":
            number = _int(str(value), _INT_NUMBER)
            result = (number is not None, number)
        case "int", "string":
            number = _int(str(value), _INT_STRING)
            result = (number is not None, number)
        case (("string" | "uri"), "string"):
            # a lone surrogate (\ud800) can't become a .NET string: recorded as a conversion error
            result = (not any(unicodedata.category(c) == "Cs" for c in str(value)), value)
        case (("string" | "uri"), "null"):
            result = (True, None)
        case "bool", ("true" | "false"):
            result = (True, kind == "true")
        case "datetime", "string":
            moment = parse_datetime(str(value))
            result = (moment is not None, moment)
    return result


def _position(data: bytes, index: int) -> tuple[int, int]:
    line = data.count(b"\n", 0, index)
    start = data.rfind(b"\n", 0, index) + 1
    return line, index - start


def bind(body: bytes, dto: Dto) -> dict[str, object]:
    """Bind a request body to ``dto`` exactly like the real API, or raise :class:`BindingError`."""
    data = body.removeprefix(b"\xef\xbb\xbf")
    if not body:
        raise BindingError("", EMPTY_BODY)
    try:
        values = _bind_root(_Reader(data), dto)
    except _ReaderError as failure:
        line, column = _position(data, failure.index)
        message = (
            f"{failure.message} Path: {failure.path} | LineNumber: {line}"
            f" | BytePositionInLine: {column}."
        )
        raise BindingError(failure.path, message) from failure
    if values is None:
        raise BindingError("", EMPTY_BODY)
    return values


def _conversion(type_name: str, path: str, index: int) -> _ReaderError:
    return _ReaderError(f"The JSON value could not be converted to {type_name}.", path, index)


def _bind_root(reader: _Reader, dto: Dto) -> dict[str, object] | None:
    i = reader.ws(0)
    if i >= reader.n:
        raise _ReaderError(_NO_TOKENS, "$", reader.n)
    first = reader.data[i]
    if first == ord("{"):
        bound, end = _bind_object(reader, i, dto)
        values: dict[str, object] | None = bound
        end = reader.ws(end)
    elif first == ord("["):
        raise _conversion(dto.clr_name, "$", i + 1)
    else:
        kind, _, end = reader.scalar(i, "$", nested=False)
        if kind != "null":
            raise _conversion(dto.clr_name, "$", end)
        values = None
        end = reader.ws(end)
    if end < reader.n:
        raise _ReaderError(
            f"'{_char(reader.data, end)}' is invalid after a single JSON value."
            " Expected end of data.",
            "$",
            end,
        )
    return values


def _bind_object(reader: _Reader, start: int, dto: Dto) -> tuple[dict[str, object], int]:
    types = {name.lower(): (name, field) for name, field in dto.fields}
    values: dict[str, object] = {name: _DEFAULTS[field] for name, field in dto.fields}
    i = reader.ws(start + 1)
    if i >= reader.n:
        raise _ReaderError(_DEPTH, "$", reader.n)
    if reader.data[i] == ord("}"):
        return values, i + 1
    while True:
        if reader.data[i] != ord('"'):
            raise _ReaderError(
                f"'{_char(reader.data, i)}' is an invalid start of a property name."
                " Expected a '\"'.",
                "$",
                i,
            )
        name, i = reader.string(i, "$")
        i = reader.colon(i, "$")
        path = f"$.{name}"
        target = types.get(name.lower())
        if target is None:
            i = reader.skip(i, path, 1)
        else:
            prop, field = target
            values[prop], i = _bind_value(reader, i, path, field)
        i = reader.ws(i)
        if i >= reader.n:
            raise _ReaderError(_DEPTH, path, reader.n)
        i, done = reader.separator(i, "$", ord("}"), "object")
        if done:
            return values, i


def _bind_value(reader: _Reader, i: int, path: str, field: FieldType) -> tuple[object, int]:
    """One property value converted to its type: (value, index after it)."""
    if reader.data[i] in b"{[":
        raise _conversion(_CLR_TYPES[field], path, i + 1)
    kind, value, end = reader.scalar(i, path, nested=True)
    ok, converted = _convert(field, kind, value)
    if not ok:
        raise _conversion(_CLR_TYPES[field], path, end)
    return converted, end


def _escape(text: str) -> str:
    out: list[str] = []
    short = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b"}
    short["\f"] = "\\f"
    for char in text:
        code = ord(char)
        if char in short:
            out.append(short[char])
        elif code > 0xFFFF:  # noqa: PLR2004
            code -= 0x10000
            out.append(f"\\u{0xD800 + (code >> 10):04X}\\u{0xDC00 + (code & 0x3FF):04X}")
        elif unicodedata.category(char) in _ESCAPED_CATEGORIES:
            out.append(f"\\u{code:04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def serialize(values: dict[str, object], dto: Dto) -> str:
    """A bound DTO written the way the real API writes it (declaration order, relaxed escaping)."""
    parts: list[str] = []
    for name, _ in dto.fields:
        value = values[name]
        if value is None:
            spelled = "null"
        elif isinstance(value, bool):
            spelled = "true" if value else "false"
        elif isinstance(value, int):
            spelled = str(value)
        else:
            spelled = _escape(str(value))
        parts.append(f"{_escape(name)}:{spelled}")
    return "{" + ",".join(parts) + "}"
