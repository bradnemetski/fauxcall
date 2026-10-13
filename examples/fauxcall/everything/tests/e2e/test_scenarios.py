"""Read-only tools: answers, argument variations and every way to refuse arguments.

Each scenario runs against the fake (default), the real server (``--target=real``) or both with a
compliance report (``--target=parity``). Inputs are mostly new (not the recorded ones), so parity
checks how the fake computes, not just what it replays.
"""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING, Any

import pytest
from everything_support import LOCAL_TIME, text_of, volatile, zod

if TYPE_CHECKING:
    from fauxcall.testing import McpClient


# -- echo -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "parity check 42",
        "Ünïcödé ✓ — ok",
        "two\nlines",
        "",
        "  padded  ",
        '{"json": [1, 2]}',
    ],
)
async def test_echo_returns_the_message(mcp: McpClient, message: str) -> None:
    result: Any = await mcp.call_tool("echo", {"message": message})
    assert result == {"content": [{"type": "text", "text": f"Echo: {message}"}]}


@pytest.mark.parametrize(
    ("arguments", "received"),
    [({}, "undefined"), ({"message": 7}, "number"), ({"message": [1]}, "array")],
)
async def test_echo_refuses_non_strings(
    mcp: McpClient, arguments: dict[str, Any], received: str
) -> None:
    result: Any = await mcp.call_tool("echo", arguments)
    assert result["isError"] is True
    assert text_of(result) == zod(
        "echo", f"Invalid input: expected string, received {received} at message"
    )


# -- get-sum --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "total"),
    [
        (17, 25, "42"),
        (-3.5, 1.25, "-2.25"),
        (0.3, 0.6, "0.8999999999999999"),
        (2e21, 3e21, "5e+21"),
        (5e-8, 0, "5e-8"),
        (1e16, 1, "10000000000000000"),
        (999999.5, 0.25, "999999.75"),
        (-0.1, 0.1, "0"),
    ],
)
async def test_get_sum(mcp: McpClient, a: float, b: float, total: str) -> None:
    result: Any = await mcp.call_tool("get-sum", {"a": a, "b": b})
    assert text_of(result).endswith(f" is {total}.")


@pytest.mark.parametrize(
    "arguments",
    [{"a": "2", "b": 3}, {"b": 3}, {"a": 1, "b": None}, {"a": [], "b": {}}, {"a": False}],
)
async def test_get_sum_refusals(mcp: McpClient, arguments: dict[str, Any]) -> None:
    result: Any = await mcp.call_tool("get-sum", arguments)
    assert result["isError"] is True
    assert text_of(result).startswith(zod("get-sum", "Invalid input: expected number"))


# -- resource links and references ----------------------------------------------------------------


@pytest.mark.parametrize("count", [4, 6, 8, 9, 1.5, 7.25])
async def test_resource_links(mcp: McpClient, count: float) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": count})
    links = result["content"][1:]
    assert len(links) == int(count)
    for index, link in enumerate(links, start=1):
        kind = "text" if index % 2 == 0 else "blob"
        assert link["uri"] == f"demo://resource/dynamic/{kind}/{index}"
        assert link["type"] == "resource_link"


@pytest.mark.parametrize("count", [0.9, 12, -2, "4", None])
async def test_resource_links_refusals(mcp: McpClient, count: Any) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": count})
    assert result["isError"] is True
    assert text_of(result).endswith(" at count")


@pytest.mark.parametrize(
    ("arguments", "uri"),
    [
        ({"resourceType": "Text", "resourceId": 5}, "demo://resource/dynamic/text/5"),
        ({"resourceType": "Blob", "resourceId": 3}, "demo://resource/dynamic/blob/3"),
        ({"resourceType": "Blob", "resourceId": 77}, "demo://resource/dynamic/blob/77"),
        ({"resourceId": 12}, "demo://resource/dynamic/text/12"),
        ({"resourceType": "Text", "resourceId": 4.0}, "demo://resource/dynamic/text/4"),
    ],
)
async def test_resource_reference(mcp: McpClient, arguments: dict[str, Any], uri: str) -> None:
    result: Any = await mcp.call_tool(
        "get-resource-reference",
        arguments,
        compare=volatile("$.content[1].resource.text", "$.content[1].resource.blob"),
    )
    resource = result["content"][1]["resource"]
    assert resource["uri"] == uri
    assert text_of(result, 2) == f"You can access this resource using the URI: {uri}"
    number = uri.rsplit("/", 1)[1]
    if "text" in resource:
        body = resource["text"]
        assert body.startswith(f"Resource {number}: This is a plaintext resource created at ")
    else:
        body = base64.b64decode(resource["blob"]).decode()
        assert body.startswith(f"Resource {number}: This is a base64 blob created at ")
    assert LOCAL_TIME.search(body)


@pytest.mark.parametrize("resource_id", [2.7, -1, 0])
async def test_resource_reference_bad_ids(mcp: McpClient, resource_id: float) -> None:
    result: Any = await mcp.call_tool("get-resource-reference", {"resourceId": resource_id})
    assert result["isError"] is True
    assert text_of(result).endswith("Must be a finite positive integer.")


@pytest.mark.parametrize(
    "arguments", [{"resourceType": "Audio"}, {"resourceId": "seven"}, {"resourceType": 1}]
)
async def test_resource_reference_refusals(mcp: McpClient, arguments: dict[str, Any]) -> None:
    result: Any = await mcp.call_tool("get-resource-reference", arguments)
    assert result["isError"] is True


# -- structured content, annotations, image, env --------------------------------------------------


@pytest.mark.parametrize("city", ["New York", "Chicago", "Los Angeles"])
async def test_structured_content(mcp: McpClient, city: str) -> None:
    result: Any = await mcp.call_tool("get-structured-content", {"location": city})
    assert json.loads(text_of(result)) == result["structuredContent"]
    assert set(result["structuredContent"]) == {"temperature", "conditions", "humidity"}


@pytest.mark.parametrize("arguments", [{"location": "Seattle"}, {"location": "CHICAGO"}, {}])
async def test_structured_content_refusals(mcp: McpClient, arguments: dict[str, Any]) -> None:
    result: Any = await mcp.call_tool("get-structured-content", arguments)
    assert text_of(result) == zod(
        "get-structured-content",
        'Invalid option: expected one of "New York"|"Chicago"|"Los Angeles" at location',
    )


@pytest.mark.parametrize(
    ("message_type", "image"), [("error", False), ("success", True), ("debug", True)]
)
async def test_annotated_message(mcp: McpClient, message_type: str, image: bool) -> None:
    result: Any = await mcp.call_tool(
        "get-annotated-message", {"messageType": message_type, "includeImage": image}
    )
    assert len(result["content"]) == (2 if image else 1)
    assert "audience" in result["content"][0]["annotations"]


@pytest.mark.parametrize(
    "arguments",
    [{"messageType": "warning"}, {"messageType": "error", "includeImage": "no"}, {"x": 1}],
)
async def test_annotated_message_refusals(mcp: McpClient, arguments: dict[str, Any]) -> None:
    result: Any = await mcp.call_tool("get-annotated-message", arguments)
    assert result["isError"] is True


async def test_tiny_image_and_env(mcp: McpClient) -> None:
    image: Any = await mcp.call_tool("get-tiny-image", {})
    assert image["content"][1]["mimeType"] == "image/png"
    assert base64.b64decode(image["content"][1]["data"]).startswith(b"\x89PNG")
    env: Any = await mcp.call_tool("get-env", {})
    assert isinstance(json.loads(text_of(env)), dict)


# -- long running operation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "text"),
    [
        ({"duration": 0.5, "steps": 2}, "Duration: 0.5 seconds, Steps: 2."),
        ({"duration": 0.2, "steps": 0}, "Duration: 0.2 seconds, Steps: 0."),
        ({"duration": 0, "steps": 4}, "Duration: 0 seconds, Steps: 4."),
    ],
)
async def test_long_running_operation(mcp: McpClient, arguments: dict[str, Any], text: str) -> None:
    result: Any = await mcp.call_tool("trigger-long-running-operation", arguments)
    assert text_of(result) == f"Long running operation completed. {text}"


async def test_long_running_operation_progress(mcp: McpClient) -> None:
    await mcp.call_tool("echo", {"message": "open the session"})
    call: Any = await mcp.wire.call(
        "tools/call",
        {
            "name": "trigger-long-running-operation",
            "arguments": {"duration": 0.6, "steps": 3},
            "_meta": {"progressToken": "parity-progress"},
        },
    )
    notes = [json.loads(data) for _, _, data, _ in call.sse_events]
    progress = [n["params"] for n in notes if n.get("method") == "notifications/progress"]
    assert progress == [
        {"progress": step, "total": 3, "progressToken": "parity-progress"} for step in (1, 2, 3)
    ]


async def test_long_running_operation_refusal(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("trigger-long-running-operation", {"duration": "slow"})
    assert text_of(result) == zod(
        "trigger-long-running-operation",
        "Invalid input: expected number, received string at duration",
    )


def test_local_time_pattern() -> None:
    assert re.search(LOCAL_TIME, "created at 6:17:52 PM")
