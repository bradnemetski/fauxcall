"""Protocol-level scenarios: ping, unknown methods, malformed requests, version negotiation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fauxcall.mcpwire.client import McpWireClient

if TYPE_CHECKING:
    from fauxcall.testing import McpClient


async def test_ping(mcp: McpClient) -> None:
    pong: Any = await mcp.request("ping")
    assert pong == {}


@pytest.mark.parametrize("method", ["tools/describe", "resources/delete", "sampling/createMessage"])
async def test_unknown_methods(mcp: McpClient, method: str) -> None:
    answer: Any = await mcp.request(method, {})
    assert answer == {"code": -32601, "message": "Method not found"}


async def test_prompt_without_a_name(mcp: McpClient) -> None:
    answer: Any = await mcp.request("prompts/get", {"arguments": {}})
    assert answer["code"] == -32603
    assert '"name"' in answer["message"]


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
async def test_protocol_versions_are_negotiated(mcp: McpClient, version: str) -> None:
    wire = McpWireClient(mcp.wire.url, mcp.wire.http, protocol_version=version)
    try:
        answer = await wire.initialize()
    finally:
        await wire.close()
    result: Any = answer.response["result"] if isinstance(answer.response, dict) else {}
    assert result["protocolVersion"] == version
    assert result["serverInfo"] == {
        "name": "mcp-servers/everything",
        "title": "Everything Reference Server",
        "version": "2.0.0",
    }
    assert result["instructions"].startswith("# Everything Server")
