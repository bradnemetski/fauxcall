"""OpenAPI document tweaks: 400 instead of 422, rate-limit headers, contract descriptions."""

from __future__ import annotations

from typing import Any, Final

from fastapi import FastAPI

from fauxcall_reference_api.ratelimit import (
    LIMIT_HEADER,
    REMAINING_HEADER,
    RESET_HEADER,
    RETRY_AFTER_HEADER,
)

DESCRIPTION: Final = """\
A deliberately rich, **deterministic** market-data API used to validate FauxCall (record a real
API, generate a fake, compare them).

* **Auth**: every `/v1` request needs `X-API-Key` (`demo-key`: full access, `readonly-key`:
  GET only, `revoked-key`: always 403). Missing/unknown key: 401.
* **Rate limit**: 30 requests per key per 10 s fixed window (starting at the window's first
  request). Authenticated responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining` and
  `X-RateLimit-Reset`; 429 adds `Retry-After`.
* **Errors**: `{"error": {"code", "message", "request_id"}}`, plus `param` for parameter errors.
  Validation errors are 400 (never 422).
* **Streaming**: `GET /v1/stream/trades` is Server-Sent Events. `WS /v1/ws/quotes?api_key=...`
  is a WebSocket quote feed (not representable in OpenAPI): send JSON text messages
  `{"action": "subscribe", "symbols": ["AAPL"]}` or `{"action": "ping"}`; the server answers with
  `subscribed`, `quote`, `pong`, `error` and `done` messages.
* **MCP**: an MCP server (Streamable HTTP, no auth) is served at `/mcp`.
"""

TAGS: Final[list[dict[str, str]]] = [
    {"name": "exchanges", "description": "Exchanges and their trading sessions."},
    {"name": "tickers", "description": "Listed instruments (cursor pagination)."},
    {"name": "quotes", "description": "Static prices with session-aware timestamps."},
    {"name": "search", "description": "Ticker search with synonyms."},
    {"name": "watchlists", "description": "Stateful CRUD (in memory, shared by all keys)."},
    {"name": "uploads", "description": "Multipart uploads processed as async jobs."},
    {"name": "streaming", "description": "Server-Sent Events."},
    {"name": "time", "description": "Server clock."},
    {"name": "health", "description": "Liveness (no auth)."},
]

HEADER_COMPONENTS: Final[dict[str, dict[str, Any]]] = {
    LIMIT_HEADER: {
        "description": "Requests allowed per window for this key.",
        "schema": {"type": "integer"},
    },
    REMAINING_HEADER: {
        "description": "Requests left in the current window.",
        "schema": {"type": "integer"},
    },
    RESET_HEADER: {
        "description": "Epoch seconds (rounded up) at which the current window ends.",
        "schema": {"type": "integer"},
    },
    RETRY_AFTER_HEADER: {
        "description": "Seconds until the window ends (integer, at least 1).",
        "schema": {"type": "integer"},
    },
}
_RATE_LIMITED_STATUSES_EXCLUDED: Final = frozenset({"401"})


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/headers/{name}"}


def _json_errors(responses: dict[str, dict[str, Any]]) -> None:
    """Error bodies are always JSON, even on routes whose success media type is not."""
    for status, response in responses.items():
        content = response.get("content")
        if status.startswith(("4", "5")) and content and "application/json" not in content:
            response["content"] = {"application/json": next(iter(content.values()))}


def _rate_limit_headers(responses: dict[str, dict[str, Any]]) -> None:
    for status, response in responses.items():
        if status in _RATE_LIMITED_STATUSES_EXCLUDED:
            continue
        headers = response.setdefault("headers", {})
        headers.update(
            {name: _ref(name) for name in (LIMIT_HEADER, REMAINING_HEADER, RESET_HEADER)}
        )
        if status == "429":
            headers[RETRY_AFTER_HEADER] = _ref(RETRY_AFTER_HEADER)


def refine_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop FastAPI's 422s, keep errors JSON, and document rate-limit headers under ``/v1``."""
    components = schema.setdefault("components", {})
    components.setdefault("headers", {}).update(HEADER_COMPONENTS)
    for name in ("HTTPValidationError", "ValidationError"):
        components.get("schemas", {}).pop(name, None)
    for path, path_item in schema.get("paths", {}).items():
        for operation in path_item.values():
            responses: dict[str, dict[str, Any]] = operation.get("responses", {})
            responses.pop("422", None)
            _json_errors(responses)
            if path.startswith("/v1/"):
                _rate_limit_headers(responses)
    return schema


class ReferenceAPI(FastAPI):
    """FastAPI with the refined OpenAPI document."""

    def openapi(self) -> dict[str, Any]:
        """Generate (once) and return the refined OpenAPI document."""
        if self.openapi_schema is None:
            self.openapi_schema = refine_openapi(super().openapi())
        return self.openapi_schema
