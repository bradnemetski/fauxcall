"""The real server's response-header middlewares, applied to every response of the fake.

Frankfurter wraps its app in Rack middlewares (``lib/app.rb``) whose headers depend on the path
and status rather than on the endpoint, so they live here instead of in per-endpoint templates:

* ``Versions::V1::Deprecation``: ``deprecation`` and a ``link`` to the v2 successor on ``/v1``.
* ``NoStoreOnError``: ``cache-control: no-store`` on every status >= 400.
* ``Noindex``: ``x-robots-tag: noindex`` except on the index documents and OpenAPI specs.
* ``Rack::Cors`` (any origin, GET/OPTIONS): ``vary: Origin``; CORS headers when ``Origin`` is sent;
  preflight ``OPTIONS`` answered directly (requested headers echoed; nothing for other methods).
* Roda routes match every verb: ``HEAD`` is answered like ``GET`` without a body, any other verb
  (``OPTIONS`` without a preflight, recorded; POST & co., from the source only) like ``GET``.
* Successful answers are cacheable for a day (``cache-control: public, max-age=86400``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEPRECATION: Final = "@1779103800"  # v2 release, 2026-05-18 11:30 UTC (RFC 9745)
SUCCESSORS: Final[dict[str, str]] = {
    "/v1": "/v2",
    "/v1/": "/v2",
    "/v1/currencies": "/v2/currencies",
    "/v1/openapi.json": "/v2/openapi.json",
}
NOINDEX_EXEMPT: Final = frozenset({"/", "/v1", "/v2", "/v1/openapi.json", "/v2/openapi.json"})
CORS: Final[tuple[tuple[str, str], ...]] = (
    ("access-control-allow-origin", "*"),
    ("access-control-allow-methods", "GET, OPTIONS"),
    ("access-control-expose-headers", ""),
    ("access-control-max-age", "7200"),
)
CACHEABLE: Final = "public, max-age=86400"
ERROR_STATUS: Final = 400


def successor_link(path: str) -> str | None:
    """The ``link`` header for a ``/v1`` path (``None`` outside v1)."""
    if path != "/v1" and not path.startswith("/v1/"):
        return None
    successor = SUCCESSORS.get(path, "/v2/rates")
    return f'<https://api.frankfurter.dev{successor}>; rel="successor-version"'


def response_headers(
    path: str, status: int, headers: list[tuple[str, str]], *, origin: bool
) -> list[tuple[str, str]]:
    """``headers`` as the real middleware stack leaves them."""
    result = [(name, value) for name, value in headers if name != "cache-control"]
    existing = dict(headers)
    if origin:
        result.extend(CORS)
    if status >= ERROR_STATUS:
        result.append(("cache-control", "no-store"))
    elif status < 300:  # noqa: PLR2004
        result.append(("cache-control", existing.get("cache-control", CACHEABLE)))
    elif "cache-control" in existing:
        result.append(("cache-control", existing["cache-control"]))
    result.append(("vary", "Origin"))
    if path not in NOINDEX_EXEMPT:
        result.append(("x-robots-tag", "noindex"))
    link = successor_link(path)
    if link is not None:
        result.append(("deprecation", DEPRECATION))
        result.append(("link", link))
    return result


CORS_METHODS: Final = frozenset({"GET", "OPTIONS"})


def _preflight_headers(
    path: str, method: str, request_headers: str | None
) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    if method.upper() in CORS_METHODS:  # Rack::Cors: no CORS headers for a disallowed method
        headers.extend(CORS)
        if request_headers:  # ``headers: :any`` echoes the requested headers
            headers.append(("access-control-allow-headers", request_headers))
    if path not in NOINDEX_EXEMPT:
        headers.append(("x-robots-tag", "noindex"))
    link = successor_link(path)
    if link is not None:
        headers.append(("deprecation", DEPRECATION))
        headers.append(("link", link))
    return headers


class RealHeaders:
    """Pure ASGI middleware adding the real server's path- and status-dependent headers."""

    def __init__(self, app: ASGIApp, *, admin_prefix: str) -> None:
        self.app = app
        self.admin_prefix = admin_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Rewrite response headers of HTTP requests (admin API untouched)."""
        if scope["type"] != "http" or scope["path"].startswith(self.admin_prefix):
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        request_headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        origin = "origin" in request_headers
        method = scope["method"]
        if method == "OPTIONS" and origin and "access-control-request-method" in request_headers:
            headers = _preflight_headers(
                path,
                request_headers["access-control-request-method"],
                request_headers.get("access-control-request-headers"),
            )
            await _send_preflight(send, headers)
            return

        head = method == "HEAD"
        if method != "GET":  # Roda's ``r.is`` matches every verb; Rack drops the body for HEAD
            scope = {**scope, "method": "GET"}

        async def send_with_headers(message: Message) -> None:
            if head and message["type"] == "http.response.body":
                message = {**message, "body": b""}
            if message["type"] == "http.response.start":
                headers = [
                    (name.decode("latin-1").lower(), value.decode("latin-1"))
                    for name, value in message.get("headers", [])
                ]
                rewritten = response_headers(path, message["status"], headers, origin=origin)
                message = {
                    **message,
                    "headers": [(n.encode("latin-1"), v.encode("latin-1")) for n, v in rewritten],
                }
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _send_preflight(send: Send, headers: list[tuple[str, str]]) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(n.encode("latin-1"), v.encode("latin-1")) for n, v in headers],
        }
    )
    await send({"type": "http.response.body", "body": b""})
