"""The HTTP side of the Streamable HTTP transport: sessions, routing, CORS (safe methods only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient

NO_SESSION = {
    "jsonrpc": "2.0",
    "error": {"code": -32000, "message": "Bad Request: No valid session ID provided"},
}


async def test_get_stream_needs_a_session(api: ApiClient) -> None:
    missing = await api.get("/mcp", headers={"Accept": "text/event-stream"})
    assert missing.status_code == 400
    assert missing.json() == NO_SESSION
    unknown = await api.get(
        "/mcp",
        headers={
            "Accept": "text/event-stream",
            "Mcp-Session-Id": "0f9d4a52-5d3e-4b8e-9f00-3a1c2b4d5e6f",
        },
    )
    assert unknown.status_code == 400
    assert unknown.json() == NO_SESSION


async def test_routing(api: ApiClient) -> None:
    unknown = await api.get("/sse")
    assert unknown.status_code == 404
    assert "<pre>Cannot GET /sse</pre>" in unknown.text
    upper = await api.get("/MCP")
    assert upper.status_code == 400
    slash = await api.get("/mcp/")
    assert slash.status_code == 400
    nested = await api.get("/mcp/extra")
    assert nested.status_code == 404


async def test_cors_preflight(api: ApiClient) -> None:
    preflight = await api.request(
        "OPTIONS",
        "/mcp",
        headers={
            "Origin": "https://inspector.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,mcp-session-id",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert preflight.headers["access-control-allow-methods"] == "GET,POST,DELETE"
    assert preflight.headers["access-control-allow-headers"] == "content-type,mcp-session-id"
    plain = await api.get("/mcp", headers={"Origin": "https://inspector.example"})
    assert plain.headers["access-control-expose-headers"] == (
        "mcp-session-id,last-event-id,mcp-protocol-version"
    )
