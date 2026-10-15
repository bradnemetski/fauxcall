"""The HTTP front: Express's routing and 404 page, CORS, and the Streamable HTTP endpoint's own
refusals. Safe methods only (GET, HEAD, OPTIONS): a raw POST would be a write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient

NO_SESSION = {
    "jsonrpc": "2.0",
    "error": {"code": -32000, "message": "Bad Request: No valid session ID provided"},
}
EXPOSED = "mcp-session-id,last-event-id,mcp-protocol-version"


def _page(shown: str) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>Error</title>\n'
        f"</head>\n<body>\n<pre>{shown}</pre>\n</body>\n</html>\n"
    )


@pytest.mark.parametrize("accept", ["text/event-stream", "application/json"])
async def test_stream_for_an_unknown_session(api: ApiClient, accept: str) -> None:
    response = await api.get(
        "/mcp", headers={"accept": accept, "mcp-session-id": "b7d6a1c2-session-nobody-issued"}
    )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == NO_SESSION


async def test_a_stream_needs_a_session_id(api: ApiClient) -> None:
    response = await api.get("/mcp", headers={"accept": "text/event-stream"})
    assert response.status_code == 400
    assert response.json() == NO_SESSION
    assert response.headers["x-powered-by"] == "Express"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-expose-headers"] == EXPOSED


@pytest.mark.parametrize("path", ["/MCP", "/mcp/", "/Mcp/?probe=1"])
async def test_the_endpoint_ignores_case_and_a_trailing_slash(api: ApiClient, path: str) -> None:
    response = await api.get(path, headers={"accept": "text/event-stream"})
    assert response.status_code == 400
    assert response.json() == NO_SESSION


async def test_a_head_takes_the_stream_route(api: ApiClient) -> None:
    response = await api.request("HEAD", "/mcp", headers={"accept": "text/event-stream"})
    assert response.status_code == 400
    assert response.content == b""
    assert response.headers["content-type"] == "application/json; charset=utf-8"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/health"),
        ("GET", "/mcp/messages"),
        ("GET", "/mcp//"),
        ("GET", "/.well-known/oauth-protected-resource"),
        ("GET", "/sse"),
    ],
)
async def test_other_paths_get_the_express_404_page(api: ApiClient, method: str, path: str) -> None:
    response = await api.request(method, path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.text == _page(f"Cannot {method} {path}")


async def test_a_head_of_an_unknown_path(api: ApiClient) -> None:
    response = await api.request("HEAD", "/nothing-here")
    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["content-length"] == str(len(_page("Cannot HEAD /nothing-here")))


@pytest.mark.parametrize("path", ["/mcp", "/anything/else"])
async def test_cors_preflight(api: ApiClient, path: str) -> None:
    response = await api.request(
        "OPTIONS",
        path,
        headers={
            "origin": "http://localhost:6274",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type,mcp-protocol-version,mcp-session-id",
        },
    )
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-allow-methods"] == "GET,POST,DELETE"
    assert (
        response.headers["access-control-allow-headers"]
        == "content-type,mcp-protocol-version,mcp-session-id"
    )
    assert response.headers["access-control-expose-headers"] == EXPOSED
    assert response.headers["vary"] == "Access-Control-Request-Headers"


async def test_a_preflight_without_requested_headers(api: ApiClient) -> None:
    response = await api.request(
        "OPTIONS", "/mcp", headers={"access-control-request-method": "GET"}
    )
    assert response.status_code == 204
    assert "access-control-allow-headers" not in response.headers
