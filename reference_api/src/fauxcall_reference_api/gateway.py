"""The API gateway: an ASGI middleware in front of every HTTP request.

For paths under ``/v1`` it applies, in order:

1. artificial latency (15-60 ms, deterministic per method + path), when enabled;
2. authentication via ``X-API-Key`` (401 missing/unknown, 403 revoked);
3. per-key rate limiting (429), adding ``X-RateLimit-*`` headers to every later response;
4. the read-only check (403 for POST/PUT/PATCH/DELETE with a read-only key).

Every HTTP request gets a request id (used in error envelopes) and one access-log line.
WebSocket and lifespan traffic passes straight through.
"""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from starlette.datastructures import Headers, MutableHeaders

from fauxcall_reference_api.errors import error_response, new_request_id
from fauxcall_reference_api.logs import get_logger
from fauxcall_reference_api.simulation import latency_ms

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from fauxcall_reference_api.context import AppContext

API_KEY_HEADER: Final = "X-API-Key"
WRITE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MS_PER_SECOND: Final = 1000

logger = get_logger("access")


def is_api_path(path: str) -> bool:
    """Whether ``path`` is under the authenticated ``/v1`` prefix."""
    return path == "/v1" or path.startswith("/v1/")


class _RejectedError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Gateway:
    """ASGI middleware implementing latency, auth, rate limits and access logging."""

    def __init__(self, app: ASGIApp, context: AppContext) -> None:
        self.app = app
        self.context = context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        extra_headers: dict[str, str] = {}
        status = 0
        started = time.perf_counter()

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                for name, value in extra_headers.items():
                    headers.append(name, value)
            await send(message)

        try:
            if is_api_path(scope["path"]):
                try:
                    await self._admit(scope, extra_headers)
                except _RejectedError as rejection:
                    response = error_response(
                        rejection.status, rejection.code, rejection.message, request_id=request_id
                    )
                    await response(scope, receive, send_with_headers)
                    return
            await self.app(scope, receive, send_with_headers)
        finally:
            elapsed_ms = (time.perf_counter() - started) * _MS_PER_SECOND
            logger.info(
                "%s %s -> %d (%.1f ms) request_id=%s",
                scope["method"],
                scope["path"],
                status,
                elapsed_ms,
                request_id,
            )

    async def _admit(self, scope: Scope, extra_headers: dict[str, str]) -> None:
        """Apply latency, auth, rate limit and the read-only check, or raise a rejection."""
        context = self.context
        method: str = scope["method"]
        if context.settings.latency:
            await context.sleep(latency_ms(method, scope["path"]) / _MS_PER_SECOND)
        key = Headers(scope=scope).get(API_KEY_HEADER)
        if not key:
            raise _RejectedError(
                HTTPStatus.UNAUTHORIZED,
                "missing_api_key",
                f"Missing API key. Send it in the {API_KEY_HEADER} header.",
            )
        access = context.settings.api_keys.get(key)
        if access is None:
            raise _RejectedError(HTTPStatus.UNAUTHORIZED, "invalid_api_key", "Invalid API key.")
        if access == "revoked":
            raise _RejectedError(
                HTTPStatus.FORBIDDEN, "api_key_revoked", "This API key has been revoked."
            )
        decision = context.limiter.hit(key, context.now_ns())
        extra_headers.update(decision.headers())
        if not decision.allowed:
            raise _RejectedError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "rate_limited",
                f"Rate limit exceeded: {decision.limit} requests per "
                f"{context.settings.rate_limit_window_seconds} seconds.",
            )
        if access == "readonly" and method in WRITE_METHODS:
            raise _RejectedError(
                HTTPStatus.FORBIDDEN,
                "read_only_key",
                f"This API key is read-only; {method} requests are not allowed.",
            )
