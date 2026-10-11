"""The fake's ASGI app."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

from fastapi import Response
from fastapi.routing import APIRoute
from fauxcall.runtime import create_app

from fauxcall_fakerestapi import aspnet
from fauxcall_fakerestapi.routes import router
from fauxcall_fakerestapi.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

SWAGGER_UI_EXCHANGE: Final = "get/000329.json"
"""The real Swagger UI page (``GET /index.html``), served as recorded."""
_JSON_TYPE: Final = b"application/json; charset=utf-8; v=1.0"
_TEXT_JSON_TYPE: Final = b"text/json; charset=utf-8; v=1.0"


def _route_methods() -> list[tuple[re.Pattern[str], set[str]]]:
    """Each served path (case-insensitive, like the real router) and its methods."""
    table: dict[str, set[str]] = {}
    for route in router.routes:
        if isinstance(route, APIRoute):
            table.setdefault(route.path, set()).update(route.methods or ())
    return [
        (re.compile(re.sub(r"\{[^/]+\}", "[^/]+", path) + "/?", re.IGNORECASE), methods)
        for path, methods in table.items()
    ]


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return bytes(value).decode("latin-1")
    return None


class AspNetQuirks:
    """What the real API's ASP.NET pipeline does around every endpoint.

    - 405 answers carry ``Allow`` (sorted, e.g. ``DELETE, GET, PUT``).
    - An unsupported ``v=`` API version in Content-Type or Accept is refused with
      ``UnsupportedApiVersion`` on every method (recorded 000313, 000319, 000320, 000331).
    - ``Accept: text/json`` gets ``text/json`` answers (recorded 000318).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.routes = _route_methods()

    def _methods(self, path: str) -> set[str]:
        allowed: set[str] = set()
        for pattern, methods in self.routes:
            if pattern.fullmatch(path):
                allowed |= methods
        return allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request with the pipeline's quirks."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        allowed = self._methods(path)
        accept = _header(scope, b"accept")
        version = aspnet.requested_version(_header(scope, b"content-type"), accept)
        if allowed and version is not None:
            await self._unsupported_version(scope, send, version)
            return
        text_json = aspnet.prefers_text_json(accept)

        async def send_adjusted(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if message["status"] == 405 and allowed:  # noqa: PLR2004
                    headers.append((b"allow", ", ".join(sorted(allowed)).encode()))
                if text_json:
                    headers = [
                        (k, _TEXT_JSON_TYPE if k == b"content-type" and v == _JSON_TYPE else v)
                        for k, v in headers
                    ]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_adjusted)

    @staticmethod
    async def _unsupported_version(scope: Scope, send: Send, version: str) -> None:
        host = _header(scope, b"host") or "localhost"
        uri = f"{scope.get('scheme', 'http')}://{host}{scope['path']}"
        message = (
            f"The HTTP resource that matches the request URI '{uri}' does not support"
            f" the API version '{version}'."
        )
        body = json.dumps(
            {"error": {"code": "UnsupportedApiVersion", "message": message, "innerError": None}},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"api-supported-versions", b"1.0"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _swagger_ui(data_dir: Path) -> str:
    exchange = json.loads((data_dir / "recordings" / SWAGGER_UI_EXCHANGE).read_text())
    return str(exchange["response"]["body"]["text"])


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (used by ``__main__`` and the tests)."""
    settings = settings or Settings()
    app = create_app(settings, routers=[router])
    page = _swagger_ui(Path(settings.data_dir))

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        """The real API redirects its root to the Swagger UI (recorded 000117)."""
        return Response(status_code=301, headers={"location": "index.html"})

    @app.get("/index.html", include_in_schema=False)
    async def swagger_ui() -> Response:
        """The Swagger UI page, as recorded."""
        return Response(page, media_type="text/html;charset=utf-8")

    app.add_middleware(AspNetQuirks)
    return app
