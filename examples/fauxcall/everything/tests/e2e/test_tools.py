"""The read-only tools: new inputs, every way their arguments are refused, progress."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from everything_support import made_now
from fauxcall.testing import CompareOptions

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject, JsonValue


def _text(result: object, index: int = 0) -> str:
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list)
    block = content[index]
    assert isinstance(block, dict)
    return str(block["text"])


@pytest.mark.parametrize(
    "message",
    [
        "Parity check: does the fake echo exactly?",
        "Ünïcödé ✓ — 日本語 🚀",
        "",
        "  surrounded by spaces  ",
        "tab\tand\nnewline",
        'quotes " and backslash \\',
        "x" * 2000,
    ],
)
async def test_echo(mcp: McpClient, message: str) -> None:
    assert _text(await mcp.call_tool("echo", {"message": message})) == f"Echo: {message}"


async def test_echo_ignores_unknown_arguments(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("echo", {"message": "kept", "extra": [1, 2], "other": None})
    assert _text(result) == "Echo: kept"


@pytest.mark.parametrize(
    "arguments",
    [{}, {"message": 3.5}, {"message": None}, {"message": {"nested": True}}, {"msg": "typo"}],
)
async def test_echo_refuses_bad_arguments(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("echo", arguments)
    assert isinstance(result, dict)
    assert result["isError"] is True


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (19, 23),
        (-7.25, 3.5),
        (0.7, 0.1),
        (1e-9, 2e-9),
        (123456789, 987654321),
        (9007199254740993, 1),
        (1e300, 1e300),
        (-1e308, -1e308),
        (2.5e21, 1),
        (0.000001, 0.0000005),
        (100.0, 0.0),
        (-0.0, -0.0),
        (1 / 3, 2 / 3),
    ],
)
async def test_get_sum(mcp: McpClient, a: float, b: float) -> None:
    text = _text(await mcp.call_tool("get-sum", {"a": a, "b": b}))
    assert text.startswith("The sum of ")


@pytest.mark.parametrize(
    "arguments",
    [{"a": "1", "b": "2"}, {"b": 4}, {"a": [], "b": {}}, {"a": None, "b": None}, {"a": True}],
)
async def test_get_sum_refuses_bad_arguments(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("get-sum", arguments)
    assert isinstance(result, dict)
    assert result["isError"] is True


@pytest.mark.parametrize("message_type", ["error", "success", "debug"])
@pytest.mark.parametrize("image", [True, False, None])
async def test_get_annotated_message(mcp: McpClient, message_type: str, image: bool | None) -> None:
    arguments: JsonObject = {"messageType": message_type}
    if image is not None:
        arguments["includeImage"] = image
    result: Any = await mcp.call_tool("get-annotated-message", arguments)
    assert isinstance(result, dict)
    assert len(result["content"]) == (2 if image else 1)


@pytest.mark.parametrize(
    "arguments",
    [
        {"messageType": "warning"},
        {"messageType": "SUCCESS", "includeImage": True},
        {"messageType": "debug", "includeImage": 1},
        {"messageType": None, "includeImage": "true"},
        {},
    ],
)
async def test_get_annotated_message_refuses(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("get-annotated-message", arguments)
    assert isinstance(result, dict)
    assert result["isError"] is True


async def test_get_env_and_tiny_image(mcp: McpClient) -> None:
    env = _text(await mcp.call_tool("get-env", {}))
    assert env.startswith("{")
    await mcp.call_tool("get-env", {"anything": "ignored"})
    image: Any = await mcp.call_tool("get-tiny-image", {})
    assert isinstance(image, dict)
    assert image["content"][1]["mimeType"] == "image/png"
    await mcp.call_tool("get-tiny-image", {"size": 3})


@pytest.mark.parametrize("count", [1, 2, 4, 6, 8, 9, 10, 1.5, 9.99])
async def test_get_resource_links(mcp: McpClient, count: float) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": count})
    assert isinstance(result, dict)
    assert len(result["content"]) == int(count) + 1


async def test_get_resource_links_default(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {})
    assert isinstance(result, dict)
    assert len(result["content"]) == 4


@pytest.mark.parametrize("count", [0.5, 10.5, -1, 100, "5", False])
async def test_get_resource_links_refuses(mcp: McpClient, count: JsonValue) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": count})
    assert isinstance(result, dict)
    assert result["isError"] is True


@pytest.mark.parametrize(
    "arguments",
    [
        {"resourceType": "Text", "resourceId": 4},
        {"resourceType": "Blob", "resourceId": 17},
        {"resourceType": "Blob", "resourceId": 2.0},
        {"resourceId": 99},
        {"resourceType": "Blob"},
        {},
    ],
)
async def test_get_resource_reference(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool(
        "get-resource-reference",
        arguments,
        compare=made_now("$.content[1].resource.text", "$.content[1].resource.blob"),
    )
    assert isinstance(result, dict)
    assert result["content"][1]["type"] == "resource"


@pytest.mark.parametrize(
    "arguments",
    [
        {"resourceType": "Text", "resourceId": 0},
        {"resourceType": "Blob", "resourceId": -10},
        {"resourceType": "Text", "resourceId": 2.5},
        {"resourceType": "Text", "resourceId": "3"},
        {"resourceType": "Audio", "resourceId": 1},
        {"resourceType": None},
    ],
)
async def test_get_resource_reference_refuses(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.call_tool("get-resource-reference", arguments, compare=CompareOptions())
    assert isinstance(result, dict)
    assert result["isError"] is True


@pytest.mark.parametrize("city", ["New York", "Chicago", "Los Angeles"])
async def test_get_structured_content(mcp: McpClient, city: str) -> None:
    result: Any = await mcp.call_tool("get-structured-content", {"location": city})
    assert isinstance(result, dict)
    assert set(result["structuredContent"]) == {"temperature", "conditions", "humidity"}


@pytest.mark.parametrize("location", ["Seattle", "new york", "", 42])
async def test_get_structured_content_refuses(mcp: McpClient, location: JsonValue) -> None:
    result: Any = await mcp.call_tool("get-structured-content", {"location": location})
    assert isinstance(result, dict)
    assert result["isError"] is True


@pytest.mark.parametrize(
    ("duration", "steps"),
    [(1, 4), (0.6, 3), (0, 2), (1.2, 2.5), (2, 1)],
)
async def test_long_running_operation_with_progress(
    mcp: McpClient, duration: float, steps: float
) -> None:
    result: Any = await mcp.call_tool(
        "trigger-long-running-operation", {"duration": duration, "steps": steps}, progress=True
    )
    assert _text(result).startswith("Long running operation completed.")


async def test_long_running_operation_without_progress(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool(
        "trigger-long-running-operation", {"duration": 0.5, "steps": 2}, notifications=True
    )
    assert "Steps: 2." in _text(result)


async def test_long_running_operation_edge_steps(mcp: McpClient) -> None:
    await mcp.call_tool(
        "trigger-long-running-operation", {"duration": 1, "steps": 0}, progress=True
    )
    await mcp.call_tool(
        "trigger-long-running-operation", {"duration": -2, "steps": 3}, progress=True
    )
    await mcp.call_tool("trigger-long-running-operation", {"steps": -1})


async def test_long_running_operation_refuses(mcp: McpClient) -> None:
    refused: list[JsonObject] = [
        {"duration": "fast"},
        {"steps": [1]},
        {"duration": None, "steps": None},
    ]
    for arguments in refused:
        result: Any = await mcp.call_tool("trigger-long-running-operation", arguments)
        assert isinstance(result, dict)
        assert result["isError"] is True
