"""What Express 5.3 and ``cors`` 2.8.6 answer and add, as the real server runs them.

``app.use(cors({origin: "*", methods: "GET,POST,DELETE", exposedHeaders: [...]}))`` answers every
``OPTIONS`` preflight itself and marks every other answer, after Express's ``X-Powered-By``
(every recording). The ``/mcp`` routes refuse a session they don't have with ``res.json`` (an
``id`` only from a parsed body, and the server parses none: 000017-000019), and anything Express
can't route gets ``finalhandler``'s HTML 404 page.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue
    from starlette.datastructures import Headers

    RawHeaders = list[tuple[bytes, bytes]]

EXPOSED: Final = b"mcp-session-id,last-event-id,mcp-protocol-version"
FRONT: Final[RawHeaders] = [
    (b"x-powered-by", b"Express"),
    (b"access-control-allow-origin", b"*"),
    (b"access-control-expose-headers", EXPOSED),
]
"""What Express and ``cors`` put on every answer, first."""
NO_SESSION: Final[JsonObject] = {
    "jsonrpc": "2.0",
    "error": {"code": -32000, "message": "Bad Request: No valid session ID provided"},
}
"""The route's refusal of a missing or unknown session (``id: req.body?.id``: none)."""

_UNSAFE_IN_URL: Final = re.compile(
    r"(?:[^\x21\x23-\x3B\x3D\x3F-\x5F\x61-\x7A\x7C\x7E]|%(?:[^0-9A-Fa-f]|[0-9A-Fa-f][^0-9A-Fa-f]|$))+"
)
_URI_RESERVED: Final = ";,/?:@&=+$!*'()#"
"""What ``encodeURI`` leaves as is, besides letters, digits and ``-_.~``."""
_HTML: Final = {'"': "&quot;", "&": "&amp;", "'": "&#39;", "<": "&lt;", ">": "&gt;"}


def preflight_headers(headers: Headers) -> RawHeaders:
    """``cors``'s answer to a preflight: the allowed methods, and the request headers echoed."""
    found: RawHeaders = [
        (b"x-powered-by", b"Express"),
        (b"access-control-allow-origin", b"*"),
        (b"access-control-allow-methods", b"GET,POST,DELETE"),
        (b"vary", b"Access-Control-Request-Headers"),
    ]
    requested = headers.get("access-control-request-headers")
    if requested:
        found.append((b"access-control-allow-headers", requested.encode("latin-1")))
    found += [(b"access-control-expose-headers", EXPOSED), (b"content-length", b"0")]
    return found


def not_found(method: str, path: str) -> tuple[RawHeaders, bytes]:
    """Express's (``finalhandler``'s) 404 page: ``Cannot GET /path``."""
    message = f"Cannot {method} {encode_url(path)}"
    escaped = "".join(_HTML.get(char, char) for char in message)
    shown = escaped.replace("\n", "<br>").replace("  ", " &nbsp;")
    page = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>Error</title>\n</head>\n<body>\n<pre>{shown}</pre>\n</body>\n</html>\n"
    ).encode()
    headers: RawHeaders = [
        *FRONT,
        (b"content-security-policy", b"default-src 'none'"),
        (b"x-content-type-options", b"nosniff"),
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(page)).encode()),
    ]
    return headers, page


def encode_url(url: str) -> str:
    """The ``encodeurl`` package: encode what isn't allowed in a URL, keeping ``%xx`` escapes."""
    return _UNSAFE_IN_URL.sub(lambda found: quote(found.group(0), safe=_URI_RESERVED), url)


def express_json(payload: JsonValue) -> tuple[RawHeaders, bytes]:
    """``res.json(payload)``: UTF-8 JSON with Express's weak ETag."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    digest = base64.b64encode(hashlib.sha1(body, usedforsecurity=False).digest()).decode()
    etag = f'W/"{len(body):x}-{digest[:27]}"'
    headers: RawHeaders = [
        *FRONT,
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
        (b"etag", etag.encode()),
    ]
    return headers, body
