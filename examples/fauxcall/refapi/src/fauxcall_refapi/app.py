"""The fake's ASGI app."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from fastapi.routing import APIRoute
from fauxcall.clock import ns_to_datetime
from fauxcall.runtime import create_app
from fauxcall.runtime.app import runtime_of_app
from fauxcall.runtime.journal import JournalEntry, body_for_journal
from fauxcall.runtime.templating import render_text
from starlette.requests import Request
from starlette.routing import Match

from fauxcall_refapi.routes import router
from fauxcall_refapi.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI, Response
    from fauxcall.model.errors import ErrorCondition
    from fauxcall.runtime import FakeRuntime, RequestContext
    from fauxcall.types import JsonValue
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

GUARDED: Final = "/v1"
"""The real API checks the key for /v1 and everything under it before it looks for a route
(000025, 000026, 000826, 000827); /v1x is not guarded (000828)."""

METHOD_ORDER: Final = ("GET", "POST", "PUT", "PATCH", "DELETE")
"""How the real API lists a path's methods in Allow (000831-000833)."""

_CONDITIONS: Final[dict[str, ErrorCondition]] = {
    "missing": "unauthorized",
    "invalid": "unauthorized",
    "forbidden": "forbidden",
}


def _guarded(path: str) -> bool:
    return path == GUARDED or path.startswith(GUARDED + "/")


def _without_body(send: Send) -> Send:
    """``send`` that drops body bytes (headers, including Content-Length, are kept)."""

    async def sender(message: Message) -> None:
        if message["type"] == "http.response.body":
            message = {**message, "body": b""}
        await send(message)

    return sender


def _with_headers(send: Send, extra: dict[str, str]) -> Send:
    """``send`` that adds ``extra`` headers to the response it starts."""

    async def sender(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = [*message.get("headers", [])]
            headers += [(k.lower().encode(), v.encode()) for k, v in extra.items()]
            message = {**message, "headers": headers}
        await send(message)

    return sender


class AuthBeforeRouting:
    """Unrouted requests (404s, 405s) behave as on the real API.

    FauxCall answers unknown routes and wrong methods generically. Under ``/v1`` the real API
    authenticates first (401 missing/unknown key, 403 revoked), then counts the call against the
    key's rate limit and answers 404/405 with the X-RateLimit-* headers and, for 405, the path's
    ``Allow`` (000033-000044, 000826-000835). Outside ``/v1`` a 405 carries ``Allow`` too, with
    no key check and no rate-limit headers (000854-000856). Routed requests pass through
    unchanged; HEAD answers carry no body, as a real server sends them (000043, 000830).
    """

    def __init__(self, app: ASGIApp, fake: FastAPI) -> None:
        self.app = app
        self.fake = fake

    def _matches(self, scope: Scope) -> tuple[bool, list[str] | None]:
        """Whether a route takes this request, and the methods of the endpoints at its path.

        ``None`` when nothing is served at the path (a 404). ``Allow`` lists only the real API's
        endpoints (routes.py), so a 405 at /openapi.json carries an empty one (000857).
        """
        if any(route.matches(scope)[0] == Match.FULL for route in self.fake.router.routes):
            return True, None
        # The fake's own top-level routes (/openapi.json); the admin API lives in an included
        # router, so it is left out.
        own = [route for route in self.fake.router.routes if isinstance(route, APIRoute)]
        if not any(route.matches(scope)[0] == Match.PARTIAL for route in [*router.routes, *own]):
            return False, None
        allowed: set[str] = set()
        for route in router.routes:
            if route.matches(scope)[0] == Match.PARTIAL:
                allowed |= set(getattr(route, "methods", None) or ())
        return False, [method for method in METHOD_ORDER if method in allowed]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate and count unrouted ``/v1`` requests before the 404/405."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] == "HEAD":
            send = _without_body(send)
        routed, allowed = self._matches(scope)
        if routed or not _guarded(str(scope["path"])):
            if allowed is not None:
                send = _with_headers(send, {"allow": ", ".join(allowed)})
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        runtime = runtime_of_app(self.fake)
        request = Request(scope, receive)
        ctx = runtime.new_context(request, None)
        extra: dict[str, str] = {}
        refused = _refusal(runtime, ctx, request)
        if refused is None:
            extra, refused = _count(runtime, ctx, scope)
        if refused is not None:
            _journal(runtime, ctx, refused, started)
            await refused(scope, receive, send)
            return
        if allowed is not None:
            extra["allow"] = ", ".join(allowed)
        await self.app(scope, receive, _with_headers(send, extra))


def _refusal(runtime: FakeRuntime, ctx: RequestContext, request: Request) -> Response | None:
    """The real 401/403 for a missing, unknown or revoked key; ``None`` for a usable key."""
    outcome = runtime.auth.authenticate(
        headers=dict(request.headers),
        query=dict(request.query_params),
        cookies=dict(request.cookies),
        now_ns=ctx.now_ns,
        required=True,
    )
    ctx.identity = outcome.identity
    auth = runtime.model.auth
    if outcome.failure not in _CONDITIONS or auth is None:
        return None
    template = {
        "missing": auth.missing_template,
        "invalid": auth.invalid_template,
        "forbidden": auth.forbidden_template,
    }[outcome.failure]
    return ctx.error(_CONDITIONS[outcome.failure], template=template)


def _count(
    runtime: FakeRuntime, ctx: RequestContext, scope: Scope
) -> tuple[dict[str, str], Response | None]:
    """Count the call against the key's limits: headers to add, and the 429 when over."""
    headers: dict[str, str] = {}
    identity = ctx.identity
    if not runtime.settings.rate_limits_enabled or identity is None or identity.anonymous:
        return headers, None
    for limit in runtime.model.rate_limits_for(f"{scope['method']} {scope['path']}"):
        if limit.key != "credential":
            continue
        status = runtime.rate_limiter.hit(limit, f"user:{identity.name}")
        values: dict[str, JsonValue] = dict(status.header_values(ctx.now_ns))
        resolver = ctx.resolver(**values)
        headers |= {name: render_text(value, resolver) for name, value in limit.headers.items()}
        if not status.allowed:
            if limit.retry_after:
                headers["retry-after"] = str(status.retry_after)
            limited = ctx.error(
                "rate_limited", template=limit.error_template, headers=headers, values=values
            )
            return headers, limited
    return headers, None


def _journal(runtime: FakeRuntime, ctx: RequestContext, response: Response, started: float) -> None:
    """Note an answer given here (the engine journals the ones it gives itself)."""
    if not runtime.journal.enabled:
        return
    request = ctx.request
    body, text = body_for_journal(bytes(response.body), response.headers.get("content-type"))
    runtime.journal.add(
        JournalEntry(
            seq=ctx.seq,
            session=ctx.session,
            request_id=ctx.request_id,
            at=ns_to_datetime(ctx.now_ns),
            method=str(request.scope.get("method", "")),
            path=request.url.path,
            query=runtime.redact(request.query_params.multi_items()),
            request_headers=runtime.redact(request.headers.items()),
            status=response.status_code,
            response_headers=runtime.redact(response.headers.items()),
            response_body=body,
            response_text=text,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            identity=ctx.identity.name if ctx.identity else None,
        )
    )


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (used by ``__main__`` and the tests)."""
    app = create_app(settings or Settings(), routers=[router])
    app.add_middleware(AuthBeforeRouting, fake=app)
    return app
