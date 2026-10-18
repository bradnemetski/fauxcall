"""The stateless tools, with new arguments: values, defaults, and every kind of refusal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from policies import STAMPED_REFERENCE

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject


def text(result: Any, index: int = 0) -> str:
    return str(result["content"][index]["text"])


@pytest.mark.parametrize(
    "message",
    [
        "parity check",
        "  spaced out\t",
        "emoji 🧪 and ñ",
        '{"json": [1, 2]}',
        "x" * 2000,
        "line\r\nbreak",
    ],
)
async def test_echo(mcp: McpClient, message: str) -> None:
    result = await mcp.call_tool("echo", {"message": message})
    assert result == {"content": [{"type": "text", "text": f"Echo: {message}"}]}


@pytest.mark.parametrize(
    "arguments",
    [{}, {"message": 7.5}, {"message": None}, {"message": False}, {"message": ["a"]}, {"msg": "x"}],
)
async def test_echo_refusals(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("echo", arguments)
    assert result["isError"] is True
    assert text(result).startswith(
        "MCP error -32602: Input validation error: Invalid arguments for tool echo: "
    )


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (19, 23, "The sum of 19 and 23 is 42."),
        (0.7, 0.1, "The sum of 0.7 and 0.1 is 0.7999999999999999."),
        (-1.25, 1.25, "The sum of -1.25 and 1.25 is 0."),
        (1e16, 1, "The sum of 10000000000000000 and 1 is 10000000000000000."),
        (2.5e-8, 0, "The sum of 2.5e-8 and 0 is 2.5e-8."),
        (6e21, 1, "The sum of 6e+21 and 1 is 6e+21."),
        (123.456, -0.456, "The sum of 123.456 and -0.456 is 123."),
    ],
)
async def test_get_sum(mcp: McpClient, a: float, b: float, expected: str) -> None:
    result = await mcp.call_tool("get-sum", {"a": a, "b": b})
    assert text(result) == expected


@pytest.mark.parametrize(
    "arguments",
    [{"a": 1}, {"b": 2}, {"a": "2", "b": "3"}, {"a": [1], "b": {}}, {"a": None, "b": True}],
)
async def test_get_sum_refusals(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("get-sum", arguments)
    assert result["isError"] is True
    assert "Invalid arguments for tool get-sum: Invalid input: expected number" in text(result)


@pytest.mark.parametrize("message_type", ["error", "success", "debug"])
@pytest.mark.parametrize("include_image", [True, False, None])
async def test_annotated_messages(
    mcp: McpClient, message_type: str, include_image: bool | None
) -> None:
    arguments: JsonObject = {"messageType": message_type}
    if include_image is not None:
        arguments["includeImage"] = include_image
    result: Any = await mcp.call_tool("get-annotated-message", arguments)
    assert result["content"][0]["type"] == "text"
    assert len(result["content"]) == (2 if include_image else 1)


@pytest.mark.parametrize(
    "arguments",
    [
        {"messageType": "warning"},
        {"messageType": None},
        {"messageType": "debug", "includeImage": 1},
    ],
)
async def test_annotated_message_refusals(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("get-annotated-message", arguments)
    assert result["isError"] is True


@pytest.mark.parametrize("count", [5, 6, 8, 9, 1.1, 9.99, 10])
async def test_resource_links(mcp: McpClient, count: float) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": count})
    links = result["content"][1:]
    assert len(links) == int(count)
    assert all(link["type"] == "resource_link" for link in links)


@pytest.mark.parametrize("count", [0.5, 10.5, -3, "five", True, None])
async def test_resource_link_refusals(mcp: McpClient, count: Any) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": count})
    assert result["isError"] is True


@pytest.mark.parametrize(
    "arguments",
    [
        {"resourceType": "Text", "resourceId": 7},
        {"resourceType": "Blob", "resourceId": 12},
        {"resourceType": "Blob", "resourceId": 4.0},
        {"resourceId": 33},
        {"resourceType": "Blob"},
        {"resourceType": "Text", "resourceId": 123456789},
    ],
)
async def test_resource_references(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool(
        "get-resource-reference", arguments, compare=STAMPED_REFERENCE
    )
    assert result["content"][1]["type"] == "resource"
    assert result["content"][2]["text"].startswith("You can access this resource using the URI: ")


@pytest.mark.parametrize(
    "arguments",
    [
        {"resourceId": 0.25},
        {"resourceId": -10},
        {"resourceType": "Blob", "resourceId": 0},
        {"resourceType": "Audio"},
        {"resourceType": 1, "resourceId": 1},
        {"resourceId": "7"},
    ],
)
async def test_resource_reference_refusals(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("get-resource-reference", arguments)
    assert result["isError"] is True


@pytest.mark.parametrize("city", ["New York", "Chicago", "Los Angeles"])
async def test_structured_content(mcp: McpClient, city: str) -> None:
    result: Any = await mcp.call_tool("get-structured-content", {"location": city, "units": "F"})
    assert set(result["structuredContent"]) == {"temperature", "conditions", "humidity"}


@pytest.mark.parametrize(
    "arguments", [{"location": "Paris"}, {"location": "CHICAGO"}, {"city": "Chicago"}]
)
async def test_structured_content_refusals(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("get-structured-content", arguments)
    assert result["isError"] is True


async def test_tiny_image_and_environment(mcp: McpClient) -> None:
    image: Any = await mcp.call_tool("get-tiny-image", {"format": "jpeg"})
    env: Any = await mcp.call_tool("get-env", {"filter": "PATH"})
    assert image["content"][1]["mimeType"] == "image/png"
    assert env["content"][0]["type"] == "text"


@pytest.mark.parametrize(
    ("arguments", "summary"),
    [
        ({"duration": 0.3, "steps": 3}, "Duration: 0.3 seconds, Steps: 3."),
        ({"duration": 0.2}, "Duration: 0.2 seconds, Steps: 5."),
        ({"duration": 0, "steps": 0}, "Duration: 0 seconds, Steps: 0."),
        ({"duration": 0.1, "steps": 0.5}, "Duration: 0.1 seconds, Steps: 0.5."),
        ({"duration": -5, "steps": 1}, "Duration: -5 seconds, Steps: 1."),
    ],
)
async def test_long_running_operation(mcp: McpClient, arguments: JsonObject, summary: str) -> None:
    result = await mcp.request(
        "tools/call",
        {
            "name": "trigger-long-running-operation",
            "arguments": arguments,
            "_meta": {"progressToken": "e2e"},
        },
        label="tools/call:trigger-long-running-operation",
    )
    assert text(result) == f"Long running operation completed. {summary}"


@pytest.mark.parametrize("arguments", [{"duration": "1"}, {"steps": None}, {"duration": [1]}])
async def test_long_running_operation_refusals(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("trigger-long-running-operation", arguments)
    assert result["isError"] is True
