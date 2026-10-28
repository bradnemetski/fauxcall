"""URLs the way the real server's ``new URL(...)`` (WHATWG) parses and spells them.

The server looks resources up by ``new URL(uri).toString()``: the scheme is lowercased, dot
segments are resolved and characters such as spaces are percent-encoded (recorded: reading
``DEMO://resource/static/document/features.md`` finds the document; ``demo://…/text/../text/8``
reads resource 8; a session resource named ``my file.gz`` can't be read back, because its
registered URI keeps the space while the lookup has ``%20``). zod's ``.url()`` accepts what
``new URL`` accepts, and ``fetch`` decodes ``data:`` URLs as the Fetch standard says.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote, unquote_to_bytes

_SPECIAL: Final[dict[str, int | None]] = {
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
    "ftp": 21,
    "file": None,
}
_SCHEME: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")
_FRAGMENT_SET: Final = frozenset(' "<>`')
_QUERY_SET: Final = frozenset(' "#<>')
_SPECIAL_QUERY_SET: Final = _QUERY_SET | {"'"}
_PATH_SET: Final = _QUERY_SET | frozenset("?`{}")
_USERINFO_SET: Final = _PATH_SET | frozenset("/:;=@[\\]^|")
_FORBIDDEN_HOST: Final = frozenset("\x00\t\n\r #/:<>?@[\\]^|")
_FORBIDDEN_DOMAIN: Final = _FORBIDDEN_HOST | frozenset("%\x7f") | {chr(c) for c in range(0x20)}
_SINGLE_DOTS: Final = frozenset({".", "%2e"})
_DOUBLE_DOTS: Final = frozenset({"..", ".%2e", "%2e.", "%2e%2e"})
_MAX_PORT: Final = 65535
_BASE64_BLOCK: Final = 4


@dataclass(frozen=True, slots=True)
class Url:
    """A parsed URL: its scheme (``protocol`` without the colon) and serialization."""

    scheme: str
    href: str
    body: str
    """Everything after ``scheme:`` up to the fragment (a ``data:`` URL's mime type and data)."""


def _encode(text: str, unsafe: frozenset[str]) -> str:
    out: list[str] = []
    for char in text:
        if char in unsafe or ord(char) < 0x20 or ord(char) > 0x7E:  # noqa: PLR2004
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
        else:
            out.append(char)
    return "".join(out)


def _resolve(path: str) -> str:
    """Remove ``.`` and ``..`` segments from an absolute path."""
    segments = path.split("/")[1:]
    kept: list[str] = []
    for position, segment in enumerate(segments):
        last = position == len(segments) - 1
        lowered = segment.lower()
        if lowered in _DOUBLE_DOTS:
            if kept:
                kept.pop()
            if last:
                kept.append("")
        elif lowered in _SINGLE_DOTS:
            if last:
                kept.append("")
        else:
            kept.append(segment)
    return "/" + "/".join(kept)


def _split_tail(rest: str) -> tuple[str, str | None, str | None]:
    """``rest`` -> (before ``?``/``#``, query, fragment)."""
    fragment = None
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
    query = None
    if "?" in rest:
        rest, query = rest.split("?", 1)
    return rest, query, fragment


def _host(raw: str, *, special: bool) -> str | None:  # noqa: PLR0911 - one per rule
    """A host as the URL keeps it, ``None`` when ``new URL`` would throw."""
    if raw.startswith("["):
        return raw.lower() if raw.endswith("]") and len(raw) > 2 else None  # noqa: PLR2004
    if not special:
        if any(char in _FORBIDDEN_HOST for char in raw):
            return None
        return _encode(raw, frozenset())
    decoded = unquote(raw)
    if not decoded:
        return None
    try:
        ascii_host = decoded.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if any(char in _FORBIDDEN_DOMAIN for char in ascii_host):
        return None
    return ascii_host


def _authority(text: str, scheme: str) -> str | None:
    """``user:pass@host:port`` serialized, ``None`` for an invalid authority."""
    special = scheme in _SPECIAL
    userinfo = ""
    if "@" in text:
        credentials, text = text.rsplit("@", 1)
        user, _, password = credentials.partition(":")
        userinfo = _encode(user, _USERINFO_SET)
        if password:
            userinfo += ":" + _encode(password, _USERINFO_SET)
        userinfo += "@" if userinfo else ""
    port = ""
    if not text.endswith("]") and ":" in text:
        text, port = text.rsplit(":", 1)
    host = _host(text, special=special)
    if host is None or (special and not host):
        return None
    if port:
        if not port.isdigit() or int(port) > _MAX_PORT:
            return None
        port = "" if int(port) == _SPECIAL.get(scheme) else f":{int(port)}"
    return userinfo + host + port


def parse(text: str) -> Url | None:
    """``new URL(text)``: the parsed URL, ``None`` when it throws ``Invalid URL``."""
    cleaned = text.strip("".join(chr(c) for c in range(0x21))).replace("\t", "")
    cleaned = cleaned.replace("\n", "").replace("\r", "")
    match = _SCHEME.match(cleaned)
    if match is None:
        return None
    scheme = match.group(1).lower()
    rest = cleaned[match.end() :]
    special = scheme in _SPECIAL
    if special:
        rest = rest.replace("\\", "/")
    head, query, fragment = _split_tail(rest)
    host: str | None = None
    if special and scheme != "file":
        stripped = head.lstrip("/")
        authority, slash, path = stripped.partition("/")
        host = _authority(authority, scheme)
        if host is None:
            return None
        path = _resolve(slash + path) if slash else "/"
        path = _encode(path, _PATH_SET)
    elif head.startswith("//"):
        authority, slash, path = head[2:].partition("/")
        host = _authority(authority, scheme) if authority else ""
        if host is None:
            return None
        path = _encode(_resolve(slash + path), _PATH_SET) if slash else ""
    elif head.startswith("/"):
        path = _encode(_resolve(head), _PATH_SET)
    else:  # an opaque path (``data:…``, ``mailto:…``): only controls are encoded
        path = _encode(head, frozenset())
    body = scheme + ":" + ("//" + host if host is not None else "") + path
    if query is not None:
        body += "?" + _encode(query, _SPECIAL_QUERY_SET if special else _QUERY_SET)
    href = body + ("#" + _encode(fragment, _FRAGMENT_SET) if fragment is not None else "")
    return Url(scheme=scheme, href=href, body=body[len(scheme) + 1 :])


def is_url(text: str) -> bool:
    """What zod's ``z.url()`` accepts: anything ``new URL`` parses."""
    return parse(text) is not None


def data_url_bytes(url: Url) -> bytes | None:
    """The bytes ``fetch`` reads from a ``data:`` URL, ``None`` where fetch fails."""
    mime, comma, payload = url.body.partition(",")
    if not comma:
        return None
    data = unquote_to_bytes(payload)
    if not re.search(r";[\x20]*base64[\x20]*$", mime.rstrip(), re.IGNORECASE):
        return data
    text = re.sub(r"[\t\n\f\r ]", "", data.decode("latin-1"))
    if len(text) % _BASE64_BLOCK == 0:
        text = text.removesuffix("=").removesuffix("=")
    if len(text) % _BASE64_BLOCK == 1 or re.search(r"[^A-Za-z0-9+/]", text):
        return None
    try:
        return base64.b64decode(text + "=" * (-len(text) % _BASE64_BLOCK), validate=True)
    except binascii.Error:  # pragma: no cover - the checks above leave valid base64 only
        return None
