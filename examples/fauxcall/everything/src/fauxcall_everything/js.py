"""JavaScript semantics the real server (Node.js) shows in its answers.

Numbers are printed the way ``String(n)`` and ``JSON.stringify`` print them (``1e+21``,
``0.30000000000000004``, ``-0`` as ``0``), values interpolated into template literals the way
``String(value)`` does, ``Number(text)`` parses the way JavaScript does, URLs parse (and
serialize) like the WHATWG ``URL`` class for the shapes this server sees, and times of day come
out like ``new Date().toLocaleTimeString()`` on the real host (en-US, America/New_York).
"""

from __future__ import annotations

import ipaddress
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, unquote_to_bytes
from zoneinfo import ZoneInfo

from fauxcall.jsonfmt import JsonFloat

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

SERVER_ZONE: Final = ZoneInfo("America/New_York")
"""The real host's time zone, as its ``toLocaleTimeString()`` answers show (``10:29:02 PM``)."""
MAX_TIMER_MS: Final = 2_147_483_647
"""Node's ``setTimeout`` treats longer (or shorter than 1 ms, or NaN) delays as 1 ms."""
_JS_WHITESPACE: Final = (
    " \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
"""What ``String.prototype.trim`` (and ``Number``) strip: JS white space and line terminators."""
_DECIMAL: Final = re.compile(r"[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)")
_RADIX: Final = re.compile(r"0([xXoObB])([0-9a-fA-F]+)")
_SPECIAL_SCHEMES: Final = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21, "file": None}
_SCHEME: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")
_FORBIDDEN_HOST: Final = frozenset(" #/:<>?@[\\]^|\x00\t\n\r")
_FORBIDDEN_DOMAIN: Final = _FORBIDDEN_HOST | frozenset("%\x7f") | {chr(c) for c in range(0x20)}
_PATH_SAFE: Final = "!$&'()*+,;=:@/%-._~[]\\^|"
"""Characters the WHATWG path percent-encode set leaves alone (besides ASCII alphanumerics)."""
_QUERY_SAFE: Final = "!$&'()*+,;=:@/?%-._~[]\\^`{}|"
_FRAGMENT_SAFE: Final = "!$&'()*+,;=:@/?%-._~[]\\^{}|#"


# -- numbers ---------------------------------------------------------------------------------------


def js_number(value: float) -> str:
    """``String(value)`` for a JavaScript number (ECMAScript ``Number::toString``)."""
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    digits, point = _shortest(abs(number))
    count = len(digits)
    if count <= point <= 21:  # noqa: PLR2004 - the spec's limit for plain integers
        body = digits + "0" * (point - count)
    elif 0 < point <= 21:  # noqa: PLR2004
        body = digits[:point] + "." + digits[point:]
    elif -6 < point <= 0:  # noqa: PLR2004 - the spec's limit for plain fractions
        body = "0." + "0" * (-point) + digits
    else:
        exponent = point - 1
        mantissa = digits[0] + ("." + digits[1:] if count > 1 else "")
        body = f"{mantissa}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"
    return sign + body


def _shortest(number: float) -> tuple[str, int]:
    """The shortest round-trip digits of a positive double, and n with value = 0.digits x 10^n."""
    mantissa, _, exp = repr(number).partition("e")
    whole, _, fraction = mantissa.partition(".")
    joined = whole + fraction
    stripped = joined.lstrip("0")
    leading = len(joined) - len(stripped)
    point = len(whole) + (int(exp) if exp else 0) - leading
    return stripped.rstrip("0") or "0", point


def as_number(value: JsonValue) -> float:
    """A JSON number as the double JavaScript holds (huge integers round, or overflow)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("not a number")
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def json_number(value: float) -> JsonValue:
    """A number to put in a JSON answer, spelled as ``JSON.stringify`` spells it."""
    if math.isnan(value) or math.isinf(value):
        return None
    if value.is_integer() and abs(value) < 1e21:  # noqa: PLR2004 - where JS switches to exponents
        return int(value)
    return JsonFloat(js_number(value))


def js_trim(text: str) -> str:
    """``String.prototype.trim``."""
    return text.strip(_JS_WHITESPACE)


def js_to_number(text: str) -> float:  # noqa: PLR0911 - one return per JS rule
    """``Number(text)``: JavaScript's string-to-number conversion (``NaN`` when it isn't one)."""
    trimmed = text.strip(_JS_WHITESPACE)
    if not trimmed:
        return 0.0
    if trimmed in {"Infinity", "+Infinity"}:
        return math.inf
    if trimmed == "-Infinity":
        return -math.inf
    radix = _RADIX.fullmatch(trimmed)
    if radix is not None:
        base = {"x": 16, "o": 8, "b": 2}[radix.group(1).lower()]
        try:
            return float(int(radix.group(2), base))
        except ValueError:
            return math.nan
    if _DECIMAL.fullmatch(trimmed):
        return float(trimmed)
    return math.nan


def is_positive_integer(number: float) -> bool:
    """``Number.isInteger(n) && n > 0`` (finite, whole, positive)."""
    return math.isfinite(number) and number.is_integer() and number > 0


def timer_seconds(seconds: float) -> float:
    """How long Node's ``setTimeout(fn, seconds * 1000)`` really waits."""
    millis = seconds * 1000
    if math.isnan(millis) or millis < 1 or millis > MAX_TIMER_MS:
        millis = 1
    return millis / 1000


# -- values as JavaScript prints them --------------------------------------------------------------


def js_string(value: JsonValue) -> str:
    """``String(value)``: what a template literal makes of a JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return js_number(as_number(value))
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None else js_string(item) for item in value)
    return "[object Object]"


def js_truthy(value: JsonValue) -> bool:
    """JavaScript truthiness of a JSON value (``undefined`` is passed as ``None`` too)."""
    if value is None or value is False:
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = as_number(value)
        return number != 0 and not math.isnan(number)
    if isinstance(value, str):
        return value != ""
    return True


def js_stringify(value: JsonValue, indent: int | None = None) -> str:
    """``JSON.stringify(value, null, indent)``: key order kept, numbers spelled as in JS."""
    return _stringify(value, indent, 0)


def _stringify(value: JsonValue, indent: int | None, depth: int) -> str:  # noqa: PLR0911
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        number = as_number(value)
        return "null" if math.isnan(number) or math.isinf(number) else js_number(number)
    if isinstance(value, str):
        return _quote(value)
    items: list[str]
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [_stringify(item, indent, depth + 1) for item in value]
        return _wrap("[", items, "]", indent, depth)
    if not value:
        return "{}"
    separator = ": " if indent else ":"
    items = [
        f"{_quote(key)}{separator}{_stringify(item, indent, depth + 1)}"
        for key, item in value.items()
    ]
    return _wrap("{", items, "}", indent, depth)


def _wrap(opening: str, items: list[str], closing: str, indent: int | None, depth: int) -> str:
    if not indent:
        return opening + ",".join(items) + closing
    inner = "\n" + " " * (indent * (depth + 1))
    outer = "\n" + " " * (indent * depth)
    return opening + inner + ("," + inner).join(items) + outer + closing


_ESCAPES: Final = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r"}
_ESCAPES["\t"] = "\\t"


def _quote(text: str) -> str:
    out = []
    for char in text:
        code = ord(char)
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif code < 0x20 or 0xD800 <= code <= 0xDFFF:  # noqa: PLR2004 - controls, lone surrogates
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


# -- time ------------------------------------------------------------------------------------------


def time_of_day(now_ns: int) -> str:
    """``new Date().toLocaleTimeString()`` on the real host (``10:29:02 PM``)."""
    moment = datetime.fromtimestamp(now_ns / 1e9, tz=SERVER_ZONE)
    hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"  # noqa: PLR2004 - noon
    return f"{hour}:{moment.minute:02d}:{moment.second:02d} {suffix}"


def iso_ms(now_ns: int) -> str:
    """``new Date().toISOString()`` (``2026-10-18T02:36:08.191Z``)."""
    moment = datetime.fromtimestamp(now_ns // 1_000_000 / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# -- URLs ------------------------------------------------------------------------------------------


class ParsedUrl:
    """What this server reads from a ``new URL(text)``: its scheme and serialization."""

    __slots__ = ("hostname", "href", "scheme")

    def __init__(self, scheme: str, hostname: str, href: str) -> None:
        self.scheme = scheme
        self.hostname = hostname
        self.href = href

    @property
    def protocol(self) -> str:
        """``url.protocol`` (``https:``)."""
        return self.scheme + ":"


def parse_url(text: str) -> ParsedUrl | None:
    """``new URL(text)`` without a base: ``None`` where it throws ``TypeError: Invalid URL``.

    Covers what WHATWG URL parsing does to the URLs this server meets (http(s), data, demo:):
    the scheme is lowercased, special URLs get a host and a path with dot segments resolved,
    and characters outside each part's allowed set are percent-encoded.
    """
    cleaned = re.sub(r"[\t\n\r]", "", text.strip("".join(chr(c) for c in range(0x21))))
    scheme_match = _SCHEME.match(cleaned)
    if scheme_match is None or cleaned[scheme_match.end() : scheme_match.end() + 1] != ":":
        return None
    scheme = scheme_match.group(0).lower()
    rest = cleaned[scheme_match.end() + 1 :]
    if scheme in _SPECIAL_SCHEMES:
        return _special(scheme, rest)
    return _non_special(scheme, rest)


def _split_tail(rest: str) -> tuple[str, str, str]:
    """``path``, ``?query`` and ``#fragment`` (each empty when absent)."""
    fragment = ""
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
        fragment = "#" + quote(fragment, safe=_FRAGMENT_SAFE)
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
        query = "?" + quote(query, safe=_QUERY_SAFE)
    return rest, query, fragment


def _dot_segments(path: str) -> str:
    """The path with ``.`` and ``..`` segments resolved (``%2e`` counting as a dot)."""
    segments = path.split("/")[1:]
    out: list[str] = []
    for index, segment in enumerate(segments):
        lowered = segment.lower()
        last = index == len(segments) - 1
        if lowered in {"..", ".%2e", "%2e.", "%2e%2e"}:
            if out:
                out.pop()
            if last:
                out.append("")
        elif lowered in {".", "%2e"}:
            if last:
                out.append("")
        else:
            out.append(segment)
    return "/" + "/".join(out)


def _authority(rest: str) -> tuple[str, str, str]:
    """``userinfo``, ``host[:port]`` and what follows the authority."""
    end = len(rest)
    for marker in "/?#":
        found = rest.find(marker)
        if found != -1:
            end = min(end, found)
    authority, tail = rest[:end], rest[end:]
    userinfo, _, hostport = authority.rpartition("@")
    return userinfo, hostport, tail


def _host_and_port(hostport: str) -> tuple[str, str] | None:
    """The host (brackets kept) and the port text; ``None`` when they can't be told apart."""
    if hostport.startswith("["):
        close = hostport.find("]")
        if close == -1:
            return None
        host, after = hostport[: close + 1], hostport[close + 1 :]
        if after and not after.startswith(":"):
            return None
        return host, after[1:]
    host, _, port = hostport.partition(":")
    return host, port


def _ipv6(host: str) -> str | None:
    """``[...]``: the address in WHATWG's (RFC 5952) spelling."""
    try:
        return f"[{ipaddress.IPv6Address(host[1:-1]).compressed}]"
    except ValueError:
        return None


def _ipv4_number(part: str) -> int | None:
    if not part:
        return None
    radix, digits = 10, part
    if part[:2].lower() == "0x":
        radix, digits = 16, part[2:]
    elif len(part) > 1 and part.startswith("0"):
        radix, digits = 8, part[1:]
    if not digits:
        return 0
    try:
        return int(digits, radix) if digits.isalnum() and digits.isascii() else None
    except ValueError:
        return None


def _ends_in_number(domain: str) -> bool:
    labels = domain.split(".")
    if labels[-1] == "" and len(labels) > 1:
        labels.pop()
    last = labels[-1]
    hexadecimal = last[:2].lower() == "0x" and all(c in "0123456789abcdefABCDEF" for c in last[2:])
    return bool(last) and ((last.isdigit() and last.isascii()) or hexadecimal)


def _ipv4(domain: str) -> str | None:
    """WHATWG's IPv4 parser (``127.1``, ``0x7f.1``); ``None`` where it fails."""
    parts = domain.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if len(parts) > 4:  # noqa: PLR2004 - four numbers at most
        return None
    numbers = [_ipv4_number(part) for part in parts]
    if any(number is None for number in numbers):
        return None
    values = [number for number in numbers if number is not None]
    if any(value > 255 for value in values[:-1]) or values[-1] >= 256 ** (5 - len(values)):  # noqa: PLR2004
        return None
    address = values[-1] + sum(value * 256 ** (3 - i) for i, value in enumerate(values[:-1]))
    return str(ipaddress.IPv4Address(address))


def _domain(host: str) -> str | None:
    """A special URL's host: percent-decoded, IDNA, then an IPv4 address if it ends in a number."""
    if host.startswith("["):
        return _ipv6(host) if host.endswith("]") else None
    try:
        decoded = unquote_to_bytes(host).decode("utf-8")
        domain = decoded.lower() if decoded.isascii() else decoded.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    domain = domain.lower()
    if not domain or any(char in _FORBIDDEN_DOMAIN for char in domain):
        return None
    return _ipv4(domain) if _ends_in_number(domain) else domain


def _port(scheme: str, port: str) -> str | None:
    """The port as serialized (``""``: none, or the scheme's default); ``None``: invalid."""
    if not port:
        return ""
    if not (port.isdigit() and port.isascii()) or int(port) > 65535:  # noqa: PLR2004
        return None
    return "" if int(port) == _SPECIAL_SCHEMES.get(scheme) else f":{int(port)}"


def _special(scheme: str, rest: str) -> ParsedUrl | None:
    rest = rest.replace("\\", "/")
    if scheme == "file":
        path, query, fragment = _split_tail(rest.lstrip("/"))
        return ParsedUrl(scheme, "", f"file:///{quote(path, safe=_PATH_SAFE)}{query}{fragment}")
    userinfo, hostport, tail = _authority(rest.lstrip("/"))
    split = _host_and_port(hostport)
    if split is None:
        return None
    hostname, port = _domain(split[0]), _port(scheme, split[1])
    if hostname is None or port is None:
        return None
    path, query, fragment = _split_tail(tail)
    path = _dot_segments(quote(path or "/", safe=_PATH_SAFE))
    credentials = quote(userinfo, safe="!$&'()*+,;=:%-._~") + "@" if userinfo else ""
    return ParsedUrl(
        scheme, hostname, f"{scheme}://{credentials}{hostname}{port}{path}{query}{fragment}"
    )


def _non_special(scheme: str, rest: str) -> ParsedUrl | None:
    if not rest.startswith("//"):  # an opaque path (data:, mailto: ...)
        path, query, fragment = _split_tail(rest)
        opaque = "".join(f"%{ord(c):02X}" if ord(c) < 0x20 or ord(c) == 0x7F else c for c in path)  # noqa: PLR2004
        return ParsedUrl(scheme, "", f"{scheme}:{opaque}{query}{fragment}")
    userinfo, hostport, tail = _authority(rest[2:])
    split = _host_and_port(hostport)
    if split is None:
        return None
    host, port = split
    if host.startswith("["):
        parsed = _ipv6(host) if host.endswith("]") else None
        if parsed is None:
            return None
        host = parsed
    elif any(char in _FORBIDDEN_HOST for char in host):
        return None
    if port and not (port.isdigit() and port.isascii() and int(port) <= 65535):  # noqa: PLR2004
        return None
    path, query, fragment = _split_tail(tail)
    if path:
        path = _dot_segments(quote(path, safe=_PATH_SAFE))
    credentials = userinfo + "@" if userinfo else ""
    netloc = credentials + quote(host, safe=_PATH_SAFE + "[]") + (f":{int(port)}" if port else "")
    return ParsedUrl(scheme, host, f"{scheme}://{netloc}{path}{query}{fragment}")
