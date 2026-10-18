"""CORS as the real server answers it (learned by MCP introspection, ``transport.cors``).

A preflight (``OPTIONS`` with ``Access-Control-Request-Method``) gets the recorded status and
headers; every other answer gets the recorded CORS headers — those seen with an ``Origin`` when
the request has one, else those seen without. ``{{origin}}`` and ``{{request_headers}}`` in a
recorded value become the request's ``Origin`` and ``Access-Control-Request-Headers``. The admin
API is left alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import Headers

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from fauxcall.types import JsonValue


class CorsAnswers:
    """Pure ASGI middleware answering CORS like the recorded server."""

    def __init__(self, app: ASGIApp, *, cors: JsonValue, admin_prefix: str) -> None:
        self.app = app
        self.admin_prefix = admin_prefix
        recorded = cors if isinstance(cors, dict) else {}
        preflight = recorded.get("preflight")
        self.preflight = preflight if isinstance(preflight, dict) else None
        self.with_origin = _pairs(recorded.get("headers"))
        self.without_origin = _pairs(recorded.get("headers_without_origin"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer a preflight, or add the CORS headers to the app's answer."""
        if scope["type"] != "http" or str(scope["path"]).startswith(self.admin_prefix):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        requested = headers.get("access-control-request-headers", "")
        if (
            scope["method"] == "OPTIONS"
            and origin
            and "access-control-request-method" in headers
            and self.preflight is not None
        ):
            status = self.preflight.get("status")
            await send(
                {
                    "type": "http.response.start",
                    "status": status if isinstance(status, int) else 204,
                    "headers": [
                        *_rendered(_pairs(self.preflight.get("headers")), origin, requested),
                        (b"content-length", b"0"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        extra = _rendered(self.with_origin if origin else self.without_origin, origin, requested)

        async def with_cors(message: Message) -> None:
            if message["type"] == "http.response.start" and extra:
                present = {name.lower() for name, _ in message.get("headers", [])}
                added = [(name, value) for name, value in extra if name not in present]
                message = {**message, "headers": [*message.get("headers", []), *added]}
            await send(message)

        await self.app(scope, receive, with_cors)


def _pairs(value: JsonValue) -> list[tuple[str, str]]:
    """Recorded ``[[name, value], …]`` headers."""
    if not isinstance(value, list):
        return []
    return [
        (str(item[0]), str(item[1]))
        for item in value
        if isinstance(item, list) and len(item) == 2  # noqa: PLR2004 - a name and a value
    ]


def _rendered(
    pairs: list[tuple[str, str]], origin: str | None, requested: str
) -> list[tuple[bytes, bytes]]:
    """Recorded headers with the request's origin and requested headers filled in."""
    return [
        (
            name.lower().encode("latin-1"),
            value.replace("{{origin}}", origin or "")
            .replace("{{request_headers}}", requested)
            .encode("latin-1"),
        )
        for name, value in pairs
    ]
