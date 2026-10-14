"""The read-only tools: listings, every argument, every refusal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from scenario_support import LOCAL_TIME, STAMPED_READ, STAMPED_REFERENCE, texts

if TYPE_CHECKING:
    from fauxcall.testing import McpClient


async def test_tool_listing(mcp: McpClient) -> None:
    listed: Any = await mcp.list_tools()
    names = [tool["name"] for tool in listed["tools"]]
    assert names[:12] == [
        "echo",
        "get-annotated-message",
        "get-env",
        "get-resource-links",
        "get-resource-reference",
        "get-structured-content",
        "get-sum",
        "get-tiny-image",
        "gzip-file-as-resource",
        "toggle-simulated-logging",
        "toggle-subscriber-updates",
        "trigger-long-running-operation",
    ]
    assert "simulate-research-query" in names
    assert "trigger-sampling-request" not in names  # only for clients that can sample


@pytest.mark.parametrize(
    "message",
    [
        "Testing 1 2 3",
        "  spaced  ",
        "tabs\tand\nnewlines",
        "emoji 🚀 ünïcödé",
        '{"json": true}',
        "a" * 500,
    ],
)
async def test_echo_variations(mcp: McpClient, message: str) -> None:
    result: Any = await mcp.call_tool("echo", {"message": message})
    assert texts(result) == [f"Echo: {message}"]


@pytest.mark.parametrize(
    "arguments",
    [{"message": True}, {"message": {"nested": 1}}, {"msg": "typo"}, {"message": 3.5}],
)
async def test_echo_refusals(mcp: McpClient, arguments: dict[str, Any]) -> None:
    result: Any = await mcp.call_tool("echo", arguments)
    assert result["isError"] is True
    assert texts(result)[0].startswith("MCP error -32602: Input validation error:")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (7, 8),
        (-3.25, 1.125),
        (1e-8, 2e-8),
        (0.7, 0.1),
        (1e16, 1),
        (123.456, -123.456),
        (5e-324, 0),
        (1.7976931348623157e308, 1e292),
        (42, -0.0001),
        (99999999999, 1),
    ],
)
async def test_sum_spells_numbers_like_javascript(mcp: McpClient, a: float, b: float) -> None:
    result: Any = await mcp.call_tool("get-sum", {"a": a, "b": b})
    assert texts(result)[0].startswith("The sum of ")


@pytest.mark.parametrize(
    "arguments",
    [{"a": [1], "b": 2}, {"a": 1, "b": {}}, {"b": "x"}, {"a": False, "b": None}],
)
async def test_sum_refusals(mcp: McpClient, arguments: dict[str, Any]) -> None:
    result: Any = await mcp.call_tool("get-sum", arguments)
    assert result["isError"] is True


@pytest.mark.parametrize(
    "arguments",
    [
        {"messageType": "success", "includeImage": False, "extra": 1},
        {"messageType": "error"},
        {"messageType": "Debug"},
        {"messageType": "success", "includeImage": 1},
        {"messageType": None},
        {"includeImage": True},
    ],
)
async def test_annotated_messages(mcp: McpClient, arguments: dict[str, Any]) -> None:
    await mcp.call_tool("get-annotated-message", arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"location": "Los Angeles", "units": "metric"},
        {"location": "new york"},
        {"location": ""},
        {"location": ["Chicago"]},
    ],
)
async def test_structured_content(mcp: McpClient, arguments: dict[str, Any]) -> None:
    await mcp.call_tool("get-structured-content", arguments)


async def test_structured_content_matches_its_text(mcp: McpClient) -> None:
    for city in ("Chicago", "Los Angeles", "New York"):
        result: Any = await mcp.call_tool("get-structured-content", {"location": city})
        assert texts(result)[0].replace(" ", "") == (
            str(result["structuredContent"]).replace("'", '"').replace(" ", "")
        )


async def test_image_and_environment(mcp: McpClient) -> None:
    image: Any = await mcp.call_tool("get-tiny-image", {"format": "png"})
    assert image["content"][1]["mimeType"] == "image/png"
    await mcp.call_tool("get-env", {"filter": "HOME"})
    annotated: Any = await mcp.call_tool(
        "get-annotated-message", {"messageType": "debug", "includeImage": True}
    )
    assert annotated["content"][1]["data"] == image["content"][1]["data"]


@pytest.mark.parametrize("count", [3, 6, 8, 9, 1.5, 9.99, 10.5, 0.5, -10, "ten"])
async def test_resource_links(mcp: McpClient, count: Any) -> None:
    await mcp.call_tool("get-resource-links", {"count": count})


async def test_resource_links_resolve(mcp: McpClient) -> None:
    """Every link points to a resource the server serves."""
    result: Any = await mcp.call_tool("get-resource-links", {"count": 4})
    for link in result["content"][1:]:
        read: Any = await mcp.request(
            "resources/read", {"uri": link["uri"]}, label="resources/read", compare=STAMPED_READ
        )
        assert read["contents"][0]["uri"] == link["uri"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"resourceType": "Blob", "resourceId": 13},
        {"resourceType": "Text", "resourceId": 250},
        {"resourceId": 1e3},
        {"resourceId": 3.0},
        {"resourceId": -0.5},
        {"resourceId": 1e-3},
        {"resourceType": "Blob", "resourceId": "x"},
        {"resourceType": "BLOB"},
        {"resourceType": None},
    ],
)
async def test_resource_reference(mcp: McpClient, arguments: dict[str, Any]) -> None:
    result: Any = await mcp.call_tool(
        "get-resource-reference", arguments, compare=STAMPED_REFERENCE
    )
    if not result.get("isError"):
        resource = result["content"][1]["resource"]
        if "text" in resource:
            stamp = resource["text"].rsplit(" created at ", 1)[1]
            assert LOCAL_TIME.match(stamp)


async def test_long_running_operation_with_a_progress_token(mcp: McpClient) -> None:
    result: Any = await mcp.request(
        "tools/call",
        {
            "name": "trigger-long-running-operation",
            "arguments": {"duration": 0.6, "steps": 3},
            "_meta": {"progressToken": "scenario-progress"},
        },
        label="tools/call:trigger-long-running-operation",
    )
    # (the progress notifications themselves are compared with recordings in
    # tests/unit/test_recorded_sessions.py; this client only sees the answer)
    assert texts(result) == ["Long running operation completed. Duration: 0.6 seconds, Steps: 3."]


@pytest.mark.parametrize(
    "arguments",
    [
        {"duration": 0.2, "steps": 4},
        {"duration": 0, "steps": 1.5},
        {"duration": True},
        {"steps": "2"},
        {"duration": 0.1, "steps": -1},
    ],
)
async def test_long_running_variations(mcp: McpClient, arguments: dict[str, Any]) -> None:
    await mcp.call_tool("trigger-long-running-operation", arguments)


async def test_unknown_tool_and_malformed_calls(mcp: McpClient) -> None:
    await mcp.call_tool("does-not-exist", {})
    await mcp.call_tool("Echo", {"message": "case matters"})
    await mcp.request("tools/call", {"arguments": {}}, label="tools/call without name")
    await mcp.request(
        "tools/call", {"name": "echo", "arguments": ["x"]}, label="tools/call array arguments"
    )


async def test_research_query_needs_a_task(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("simulate-research-query", {"topic": "tides"})
    assert result["isError"] is True


async def test_ping_and_unknown_method(mcp: McpClient) -> None:
    assert await mcp.request("ping", {}, label="ping") == {}
    await mcp.request("tools/unknown", {}, label="unknown method")
    await mcp.request("tasks/list", {}, label="tasks/list empty")
