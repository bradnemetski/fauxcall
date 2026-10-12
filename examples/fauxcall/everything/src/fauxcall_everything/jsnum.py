"""JavaScript semantics the real server (Node.js) shows in its answers.

The Everything server formats numbers with ``String(number)``, parses resource ids with
``Number(string)``, normalizes URIs with ``new URL(uri)`` and prints times with
``toLocaleTimeString()``; these helpers reproduce exactly what was recorded.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from urllib.parse import quote
from zoneinfo import ZoneInfo

_JS_MAX_FIXED: Final = 21
_JS_MIN_FIXED: Final = -6
_DECIMAL_LITERAL: Final = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_RADIX_LITERAL: Final = re.compile(r"0([xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)")
_RADIX: Final = {"x": 16, "o": 8, "b": 2}
_JS_WHITESPACE: Final = (
    " \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
"""What ``String.prototype.trim`` (and so ``Number()``) strips."""
_SCHEME: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")
_SPECIAL_SCHEMES: Final = frozenset({"http", "https", "ws", "wss", "ftp", "file"})
_PATH_SAFE: Final = "!$&'()*+,-./:;=@[]^_|~%\\"
_QUERY_SAFE: Final = "!$&'()*+,-./:;=?@[]^_`{|}~%\\"
_FRAGMENT_SAFE: Final = "!#$&'()*+,-./:;=?@[]^_{|}~%\\"


def js_string(value: float) -> str:
    """``String(value)`` for a JavaScript number (``0.30000000000000004``, ``1e+21``, ``4``)."""
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    # repr() is the shortest round-tripping form, like JavaScript's; only the layout differs
    shortest = Decimal(repr(abs(number))).normalize().as_tuple()
    assert isinstance(shortest.exponent, int)  # noqa: S101 - finite numbers only
    digits = "".join(map(str, shortest.digits))
    count = len(digits)
    point = shortest.exponent + count  # digits before the decimal point (may be <= 0)
    if count <= point <= _JS_MAX_FIXED:
        text = digits + "0" * (point - count)
    elif 0 < point <= _JS_MAX_FIXED:
        text = f"{digits[:point]}.{digits[point:]}"
    elif _JS_MIN_FIXED < point <= 0:
        text = "0." + "0" * -point + digits
    else:
        power = point - 1
        mantissa = digits[0] + (f".{digits[1:]}" if count > 1 else "")
        text = f"{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"
    return sign + text


def js_number(text: str) -> float:
    """``Number(text)``: whitespace trimmed, ``""`` is 0, hex/octal/binary literals, else NaN."""
    stripped = text.strip(_JS_WHITESPACE)
    infinities = {"Infinity": math.inf, "+Infinity": math.inf, "-Infinity": -math.inf}
    radix = _RADIX_LITERAL.fullmatch(stripped)
    if not stripped:
        number = 0.0
    elif stripped in infinities:
        number = infinities[stripped]
    elif radix is not None:
        number = float(int(radix.group(1)[1:], _RADIX[radix.group(1)[0].lower()]))
    elif _DECIMAL_LITERAL.fullmatch(stripped):
        number = float(stripped)
    else:
        number = math.nan
    return number


def is_positive_integer(number: float) -> bool:
    """``Number.isFinite(n) && Number.isInteger(n) && n > 0``."""
    return math.isfinite(number) and number.is_integer() and number > 0


def js_typeof(value: object, *, present: bool = True) -> str:
    """The type name Zod reports as ``received``."""
    if not present:
        return "undefined"
    names: tuple[tuple[type | tuple[type, ...], str], ...] = (
        (type(None), "null"),
        (bool, "boolean"),
        ((int, float), "number"),
        (str, "string"),
        (list, "array"),
    )
    return next((name for kind, name in names if isinstance(value, kind)), "object")


class InvalidUrlError(ValueError):
    """``new URL(text)`` throws ``TypeError: Invalid URL``."""


_DEFAULT_PORTS: Final = {"http": "80", "https": "443", "ws": "80", "wss": "443", "ftp": "21"}


def clean_url(text: str) -> str:
    """What the URL parser reads: surrounding C0 controls and spaces trimmed, tabs/newlines gone."""
    return text.strip("".join(chr(c) for c in range(0x21))).translate(
        {ord("\t"): None, ord("\n"): None, ord("\r"): None}
    )


def _special_authority(scheme: str, authority: str) -> str:
    """Host lowercased, the scheme's default port dropped."""
    host, colon, port = authority.lower().rpartition(":")
    if colon and port == _DEFAULT_PORTS.get(scheme):
        return host
    return authority.lower()


_FORBIDDEN_HOST: Final = frozenset(" #/<>?@[\\]^|%")
_MAX_PORT: Final = 65535


def _check_host(authority: str, text: str) -> None:
    """Forbidden host code points or a bad port make ``new URL`` throw (recorded)."""
    host, colon, port = authority.rpartition(":") if ":" in authority else (authority, "", "")
    if any(char in _FORBIDDEN_HOST for char in host):
        raise InvalidUrlError(text)
    if colon and port and (not port.isdigit() or int(port) > _MAX_PORT):
        raise InvalidUrlError(text)


def _remove_dot_segments(path: str) -> str:
    """``.`` and ``..`` segments (also ``%2e``) resolved, as for every hierarchical URL."""
    output: list[str] = []
    segments = path.split("/")
    for index, segment in enumerate(segments):
        dots = segment.lower().replace("%2e", ".")
        last = index == len(segments) - 1
        if dots == "..":
            if output:
                output.pop()
            if last:
                output.append("")
        elif dots == ".":
            if last:
                output.append("")
        else:
            output.append(segment)
    return "/".join(output)


def normalize_url(text: str) -> str:
    """``new URL(text).href`` for the URIs this server sees (``demo://…``, ``http(s)://…``).

    Scheme lowercased; surrounding C0 controls and spaces trimmed, tabs and newlines dropped;
    spaces and other characters outside the WHATWG percent-encode sets escaped. Special
    schemes (http …) need a host and get it lowercased.
    """
    cleaned = clean_url(text)
    scheme, colon, rest = cleaned.partition(":")
    if not colon or not _SCHEME.fullmatch(scheme):
        raise InvalidUrlError(text)
    scheme = scheme.lower()
    rest, hash_mark, fragment = rest.partition("#")
    rest, question, query = rest.partition("?")
    if rest.startswith("//"):
        authority, slash, path = rest[2:].partition("/")
        if scheme in _SPECIAL_SCHEMES:
            if not authority and scheme != "file":
                raise InvalidUrlError(text)
            authority, slash = _special_authority(scheme, authority), "/"  # path is at least /
        head = f"//{authority}{slash}"
    elif scheme in _SPECIAL_SCHEMES:  # http:x is http://x/
        authority, slash, path = rest.lstrip("/").partition("/")
        if not authority:
            raise InvalidUrlError(text)
        head = f"//{_special_authority(scheme, authority)}/"
    else:
        head, path = "", rest
    if head:
        _check_host(head[2:].rstrip("/"), text)
        path = _remove_dot_segments(path)
    href = f"{scheme}:{head}{quote(path, safe=_PATH_SAFE)}"
    if question:
        href += "?" + quote(query, safe=_QUERY_SAFE)
    if hash_mark:
        href += "#" + quote(fragment, safe=_FRAGMENT_SAFE)
    return href


def local_time(now_ns: int, zone: str) -> str:
    """``new Date().toLocaleTimeString()`` in en-US: ``11:31:23 PM``, ``1:05:09 AM``."""
    moment = datetime.fromtimestamp(now_ns / 1e9, UTC).astimezone(ZoneInfo(zone))
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment:%M:%S} {'AM' if moment.hour < 12 else 'PM'}"  # noqa: PLR2004


def iso_ms(now_ns: int) -> str:
    """``new Date().toISOString()``: millisecond precision, ``Z``."""
    millis = now_ns // 1_000_000
    moment = datetime.fromtimestamp(millis // 1000, UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{millis % 1000:03d}Z"
