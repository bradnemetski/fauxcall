"""The WHATWG URL parsing Node.js does (``new URL(text)`` and its ``toString()``).

The real server checks URLs with ``URL.canParse`` (zod's ``z.url()``) and looks resources up by
``new URL(uri).toString()``, so ``demo://resource/session/My File.gz`` is looked up as
``…/My%20File.gz``. This is the subset of the standard those paths need: schemes, special and
opaque hosts, ports, dot segments and percent-encoding.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Final

SPECIAL: Final = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21, "file": None}
_SCHEME: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")
_FORBIDDEN_HOST: Final = frozenset("\x00\t\n\r #/:<>?@[\\]^|")
_FORBIDDEN_DOMAIN: Final = _FORBIDDEN_HOST | frozenset("%\x7f") | frozenset(map(chr, range(32)))
_C0: Final = frozenset(map(chr, range(32)))
_FRAGMENT_SET: Final = _C0 | frozenset(' "<>`')
_QUERY_SET: Final = _C0 | frozenset(' "#<>')
_SPECIAL_QUERY_SET: Final = _QUERY_SET | frozenset("'")
_PATH_SET: Final = _QUERY_SET | frozenset("?^`{}")
_USERINFO_SET: Final = _PATH_SET | frozenset("/:;=@[\\]|")
_MAX_PORT: Final = 65535
_HEX: Final = 16


class InvalidUrlError(ValueError):
    """``TypeError: Invalid URL``."""


@dataclass(slots=True)
class Url:
    """A parsed URL's parts, as the standard keeps them."""

    scheme: str
    userinfo: str | None
    host: str | None
    port: str | None
    path: list[str] | str
    query: str | None
    fragment: str | None

    def serialize(self) -> str:
        """``url.href``."""
        text = self.scheme + ":"
        if self.host is not None:
            text += "//"
            if self.userinfo:
                text += self.userinfo + "@"
            text += self.host
            if self.port is not None:
                text += ":" + self.port
        if isinstance(self.path, str):
            text += self.path
        else:
            if self.host is None and len(self.path) > 1 and self.path[0] == "":
                text += "/."
            text += "".join("/" + segment for segment in self.path)
        if self.query is not None:
            text += "?" + self.query
        if self.fragment is not None:
            text += "#" + self.fragment
        return text


def _encode(text: str, keep_encoded: frozenset[str]) -> str:
    """Percent-encode code points in ``keep_encoded`` and beyond ASCII (UTF-8)."""
    out: list[str] = []
    for char in text:
        if char in keep_encoded or ord(char) > 0x7E:  # noqa: PLR2004 - '~', the last ASCII kept
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
        else:
            out.append(char)
    return "".join(out)


def _decode(text: str) -> bytes:
    """Percent-decode to bytes (invalid escapes stay as they are)."""
    raw = text.encode("utf-8", "surrogatepass")
    out = bytearray()
    index = 0
    while index < len(raw):
        if raw[index] == ord("%") and index + 2 < len(raw) and _is_hex(raw[index + 1 : index + 3]):
            out.append(int(raw[index + 1 : index + 3], _HEX))
            index += 3
        else:
            out.append(raw[index])
            index += 1
    return bytes(out)


def _is_hex(pair: bytes) -> bool:
    return len(pair) == 2 and all(chr(b) in "0123456789abcdefABCDEF" for b in pair)  # noqa: PLR2004


def _ipv4_number(part: str) -> int | None:
    if not part:
        return None
    base = 10
    lowered = part.lower()
    if lowered.startswith("0x"):
        base, part = _HEX, part[2:]
    elif len(part) > 1 and part.startswith("0"):
        base, part = 8, part[1:]
    if not part:
        return 0
    try:
        return int(part, base)
    except ValueError:
        return None


def _ends_in_number(domain: str) -> bool:
    parts = domain.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    last = parts[-1]
    if last and all(c.isdigit() for c in last):
        return True
    return _ipv4_number(last) is not None and last.lower().startswith("0x")


def _ipv4(domain: str) -> str:
    parts = domain.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if len(parts) > 4:  # noqa: PLR2004 - a dotted quad at most
        raise InvalidUrlError(domain)
    numbers = [_ipv4_number(part) for part in parts]
    if any(number is None for number in numbers):
        raise InvalidUrlError(domain)
    values = [number for number in numbers if number is not None]
    if any(value > 255 for value in values[:-1]):  # noqa: PLR2004
        raise InvalidUrlError(domain)
    if values[-1] >= 256 ** (5 - len(values)):
        raise InvalidUrlError(domain)
    address = values[-1]
    for index, value in enumerate(values[:-1]):
        address += value * 256 ** (3 - index)
    return str(ipaddress.IPv4Address(address))


def _ipv6_text(packed: bytes) -> str:
    """The standard's IPv6 serializer: hex pieces, the first longest run of zeros as ``::``."""
    pieces = [int.from_bytes(packed[index : index + 2], "big") for index in range(0, 16, 2)]
    best_start, best_length, start = -1, 1, None
    for index, piece in enumerate([*pieces, 1]):
        if piece == 0 and start is None:
            start = index
        elif piece != 0 and start is not None:
            if index - start > best_length:
                best_start, best_length = start, index - start
            start = None
    if best_start < 0:
        return ":".join(f"{piece:x}" for piece in pieces)
    head = ":".join(f"{piece:x}" for piece in pieces[:best_start])
    tail = ":".join(f"{piece:x}" for piece in pieces[best_start + best_length :])
    return f"{head}::{tail}"


def _special_host(text: str) -> str:
    if text.startswith("["):
        if not text.endswith("]"):
            raise InvalidUrlError(text)
        try:
            packed = ipaddress.IPv6Address(text[1:-1]).packed
        except ValueError as error:
            raise InvalidUrlError(text) from error
        return "[" + _ipv6_text(packed) + "]"
    try:
        domain = _decode(text).decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidUrlError(text) from error
    if domain.isascii():
        ascii_domain = domain.lower()
    else:
        try:
            ascii_domain = domain.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise InvalidUrlError(text) from error
    if not ascii_domain or any(char in _FORBIDDEN_DOMAIN for char in ascii_domain):
        raise InvalidUrlError(text)
    return _ipv4(ascii_domain) if _ends_in_number(ascii_domain) else ascii_domain


def _opaque_host(text: str) -> str:
    if text.startswith("["):
        return _special_host(text)
    if any(char in _FORBIDDEN_HOST for char in text):
        raise InvalidUrlError(text)
    return _encode(text, _C0)


def _split_authority(rest: str, *, special: bool) -> tuple[str, str]:
    """The authority and what follows it (path, query, fragment)."""
    stops = "/?#\\" if special else "/?#"
    for index, char in enumerate(rest):
        if char in stops:
            return rest[:index], rest[index:]
    return rest, ""


def _port(text: str, scheme: str) -> str | None:
    if text == "":
        return None
    if not text.isdigit() or int(text) > _MAX_PORT:
        raise InvalidUrlError(text)
    number = int(text)
    return None if SPECIAL.get(scheme) == number else str(number)


def _path_segments(text: str, *, special: bool) -> list[str]:
    if special:
        text = text.replace("\\", "/")
    segments: list[str] = []
    pieces = text.split("/")
    if pieces and pieces[0] == "":
        pieces = pieces[1:]
    for index, piece in enumerate(pieces):
        last = index == len(pieces) - 1
        lowered = piece.lower()
        if lowered in {"..", ".%2e", "%2e.", "%2e%2e"}:
            if segments:
                segments.pop()
            if last:
                segments.append("")
        elif lowered in {".", "%2e"}:
            if last:
                segments.append("")
        else:
            segments.append(_encode(piece, _PATH_SET))
    return segments


def _split_query_fragment(rest: str, *, special: bool) -> tuple[str, str | None, str | None]:
    """What precedes ``?``/``#``, the query and the fragment (percent-encoded)."""
    fragment: str | None = None
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
        fragment = _encode(fragment, _FRAGMENT_SET)
    if special:
        rest = rest.replace("\\", "/")
    query: str | None = None
    if "?" in rest:
        rest, query = rest.split("?", 1)
        query = _encode(query, _SPECIAL_QUERY_SET if special else _QUERY_SET)
    return rest, query, fragment


def _with_authority(
    scheme: str, rest: str, *, special: bool
) -> tuple[str | None, str, str | None, list[str]]:
    """``(userinfo, host, port, path)`` of a URL with an authority (``scheme://…``)."""
    authority, rest = _split_authority(rest.lstrip("/") if special else rest[2:], special=special)
    userinfo: str | None = None
    if "@" in authority:
        raw_userinfo, authority = authority.rsplit("@", 1)
        user, separator, password = raw_userinfo.partition(":")
        userinfo = _encode(user, _USERINFO_SET) + separator + _encode(password, _USERINFO_SET)
    host_text, port_text = authority, ""
    colon = authority.rfind(":")
    if colon > authority.rfind("]"):
        host_text, port_text = authority[:colon], authority[colon + 1 :]
    if special and host_text == "":
        raise InvalidUrlError(authority)
    host = _special_host(host_text) if special else _opaque_host(host_text)
    path = _path_segments(rest, special=special) if special or rest else []
    return userinfo, host, _port(port_text, scheme), path


def parse(text: str) -> Url:
    """``new URL(text)`` (no base): the parsed URL, or :class:`InvalidUrlError`."""
    text = re.sub(r"[\t\n\r]", "", text.strip("".join(map(chr, range(33)))))
    match = _SCHEME.match(text)
    if match is None or text[match.end() : match.end() + 1] != ":":
        raise InvalidUrlError(text)
    scheme = match.group(0).lower()
    special = scheme in SPECIAL
    rest, query, fragment = _split_query_fragment(text[match.end() + 1 :], special=special)
    if scheme == "file":
        host = ""
        if rest.startswith("//"):
            authority, rest = _split_authority(rest[2:], special=True)
            host = "" if authority in {"", "localhost"} else _special_host(authority)
        return Url(scheme, None, host, None, _path_segments(rest, special=True), query, fragment)
    if special or rest.startswith("//"):
        userinfo, host, port, path = _with_authority(scheme, rest, special=special)
        return Url(scheme, userinfo, host, port, path, query, fragment)
    if rest.startswith("/"):
        return Url(scheme, None, None, None, _path_segments(rest, special=False), query, fragment)
    return Url(scheme, None, None, None, _encode(rest, _C0), query, fragment)


def is_valid_url(text: str) -> bool:
    """``URL.canParse(text)``."""
    try:
        parse(text)
    except InvalidUrlError:
        return False
    return True


def href(text: str) -> str:
    """``new URL(text).toString()`` (:class:`InvalidUrlError` when it can't be parsed)."""
    return parse(text).serialize()
