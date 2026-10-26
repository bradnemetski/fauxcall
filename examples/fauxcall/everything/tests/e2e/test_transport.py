"""The HTTP layer under MCP: how the server refuses requests before any JSON-RPC handling.

Only reads (GET, OPTIONS) are compared with the real server here: a raw POST, DELETE or PUT is
a write the approval doesn't cover. Their refusals are recorded by introspection and checked
against those recordings in ``tests/unit/test_transport_refusals.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient

STALE_SESSION = "0b34feaa-9571-49de-839c-6b562b3b2dfa"


@pytest.mark.parametrize(
    "headers",
    [
        {"accept": "text/event-stream", "mcp-session-id": STALE_SESSION},
        {"accept": "text/event-stream"},
        {"accept": "application/json"},
        {},
        {"mcp-session-id": STALE_SESSION, "mcp-protocol-version": "1999-01-01"},
    ],
)
async def test_stream_needs_a_session(api: ApiClient, headers: dict[str, str]) -> None:
    response: Any = await api.get("/mcp", headers=headers)
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/MCP", "/mcp/", "/Mcp/"])
async def test_mcp_path_matches_any_case_and_trailing_slash(api: ApiClient, path: str) -> None:
    response: Any = await api.get(path, headers={"accept": "text/event-stream"})
    assert response.status_code == 400


@pytest.mark.parametrize(
    "path", ["/not-mcp", "/mcp/extra", "/sse", "/.well-known/oauth-protected-resource"]
)
async def test_unknown_routes(api: ApiClient, path: str) -> None:
    response: Any = await api.get(path)
    assert response.status_code == 404


async def test_cors_preflight(api: ApiClient) -> None:
    response: Any = await api.request(
        "OPTIONS",
        "/mcp",
        headers={
            "origin": "https://inspector.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type,mcp-session-id",
        },
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"


async def test_cors_on_answers(api: ApiClient) -> None:
    response: Any = await api.get("/mcp", headers={"origin": "https://inspector.example"})
    assert response.headers["access-control-allow-origin"] == "*"
