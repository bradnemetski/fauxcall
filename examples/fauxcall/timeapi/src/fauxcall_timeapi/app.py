"""The fake's ASGI app."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fauxcall.runtime import create_app
from fauxcall.runtime.app import runtime_of_app

from fauxcall_timeapi.routes import router
from fauxcall_timeapi.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

METHOD_NOT_ALLOWED = 405


class AllowHeader:
    """Answer 405s with the methods the path really accepts (``Allow: POST`` on POST routes).

    The engine's 405 template is API-wide; the real server names the route's own methods
    (recordings 000008, 000514).
    """

    def __init__(self, app: ASGIApp, routes: list[tuple[re.Pattern[str], str]]) -> None:
        self.app = app
        self.routes = routes

    def allowed(self, path: str) -> str | None:
        """Methods accepted at ``path`` (case-insensitive, trailing slash ignored)."""
        wanted = path.rstrip("/").lower() or "/"
        methods = sorted({method for pattern, method in self.routes if pattern.fullmatch(wanted)})
        return ", ".join(methods) or None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        allow = self.allowed(scope.get("path", "/"))

        async def send_with_allow(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == METHOD_NOT_ALLOWED:
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"allow"]
                if allow:
                    headers.append((b"allow", allow.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_allow)


def _route_patterns(app: FastAPI) -> list[tuple[re.Pattern[str], str]]:
    patterns: list[tuple[re.Pattern[str], str]] = []
    for endpoint in runtime_of_app(app).model.endpoints:
        template = endpoint.path.rstrip("/").lower() or "/"
        regex = re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template))
        patterns.append((re.compile(regex), endpoint.method.value))
    return patterns


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (used by ``__main__`` and the tests)."""
    app = create_app(settings or Settings(), routers=[router])
    app.add_middleware(AllowHeader, routes=_route_patterns(app))
    return app
