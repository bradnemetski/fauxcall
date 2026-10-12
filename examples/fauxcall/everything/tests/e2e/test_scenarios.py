"""End-to-end scenarios for this fake.

Each runs against the fake (default), the real API (``--target=real``) or both with a compliance
report (``--target=parity``). Write many: happy paths, parameter variations, errors, cross-endpoint
consistency (every reference resolves), time behavior, pagination walks, async jobs, auth failures.
Writes to the real API additionally need ``--allow-real-writes``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fauxcall.testing import McpClient


async def test_recorded_mcp_tools_call_echo(mcp: McpClient) -> None:
    result = await mcp.call_tool("echo", {"message": "Hello from FauxCall"})
    assert result is not None


async def test_recorded_mcp_tools_call_get_annotated_message(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-annotated-message", {"messageType": "success"})
    assert result is not None


async def test_recorded_mcp_tools_call_get_env(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-env", {})
    assert result is not None


async def test_recorded_mcp_tools_call_get_resource_links(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-resource-links", {})
    assert result is not None


async def test_recorded_mcp_tools_call_get_resource_referenc(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-resource-reference", {})
    assert result is not None


async def test_recorded_mcp_tools_call_get_structured_conten(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-structured-content", {"location": "Chicago"})
    assert result is not None


async def test_recorded_mcp_tools_call_get_sum(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-sum", {"a": 2, "b": 3})
    assert result is not None


async def test_recorded_mcp_tools_call_get_tiny_image(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-tiny-image", {})
    assert result is not None


async def test_recorded_mcp_tools_call_trigger_long_running(mcp: McpClient) -> None:
    result = await mcp.call_tool("trigger-long-running-operation", {"duration": 2, "steps": 2})
    assert result is not None


async def test_recorded_mcp_tools_call_get_roots_list(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-roots-list", {})
    assert result is not None


async def test_recorded_mcp_tools_call_gzip_file_as_resource(mcp: McpClient) -> None:
    result = await mcp.call_tool(
        "gzip-file-as-resource",
        {"name": "hello.txt.gz", "data": "data:text/plain;base64,SGVsbG8sIFdvcmxkIQ=="},
    )
    assert result is not None


async def test_recorded_mcp_tools_call_toggle_simulated_logg(mcp: McpClient) -> None:
    result = await mcp.call_tool("toggle-simulated-logging", {})
    assert result is not None
