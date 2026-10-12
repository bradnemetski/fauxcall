"""Scenarios for the stateless tools: new argument values, every validation error, consistency.

Each assertion holds on the real server and on the fake; parity compares the answers value by
value (the time inside dynamic resources is checked here instead, see ``CLOCK_TEXT``).
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

import pytest
from fauxcall.model.fields import IgnorePolicy
from fauxcall.testing.client import CompareOptions

from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

CLOCK_TEXT: Final = CompareOptions(
    policies=[
        IgnorePolicy(path="content[*].resource.text", scope="body"),
        IgnorePolicy(path="content[*].resource.blob", scope="body"),
    ]
)
"""Dynamic resources print the server's local time: asserted by the scenarios, not compared."""
VALIDATION: Final = "MCP error -32602: Input validation error: Invalid arguments for tool "


def text(result: Any, index: int = 0) -> str:
    return str(result["content"][index]["text"])


def assert_recent_local_time(stamp: str) -> None:
    """``h:mm:ss AM`` close to now in the server's zone (both sides run within seconds)."""
    zone = ZoneInfo(Settings().local_tz)
    now = datetime.now(UTC).astimezone(zone)
    candidates = [
        (now + timedelta(seconds=delta)).strftime("%-I:%M:%S %p") for delta in range(-15, 3)
    ]
    assert stamp in candidates, f"{stamp} is not the local time ({candidates[-3]})"


# -- echo ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    ["Testing 1, 2, 3", "  padded  ", "émoji 🚀 and ünïcode", "a" * 300, "line\r\nbreak", "{}"],
)
async def test_echo_returns_the_message(mcp: McpClient, message: str) -> None:
    result: Any = await mcp.call_tool("echo", {"message": message})
    assert result == {"content": [{"type": "text", "text": f"Echo: {message}"}]}


async def test_echo_ignores_unknown_arguments(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("echo", {"message": "kept", "loud": True, "times": 3})
    assert text(result) == "Echo: kept"


@pytest.mark.parametrize(
    ("value", "received"),
    [
        (7.5, "number"),
        (False, "boolean"),
        (None, "null"),
        ([1, 2], "array"),
        ({"x": 1}, "object"),
    ],
)
async def test_echo_refuses_a_message_that_is_not_a_string(
    mcp: McpClient, value: Any, received: str
) -> None:
    result: Any = await mcp.call_tool("echo", {"message": value})
    assert result["isError"] is True
    assert text(result) == (
        f"{VALIDATION}echo: Invalid input: expected string, received {received} at message"
    )


async def test_echo_without_a_message(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("echo", {})
    assert text(result).endswith("expected string, received undefined at message")


# -- get-sum -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "total"),
    [
        (7.25, 0.5, "7.75"),
        (-12, 12, "0"),
        (0.7, 0.1, "0.7999999999999999"),
        (1e-7, 0, "1e-7"),
        (123456.789, 0.001, "123456.79000000001"),
        (9007199254740992, 1, "9007199254740992"),
        (5e20, 5e20, "1e+21"),
        (-0.5, -0.25, "-0.75"),
    ],
)
async def test_sum_prints_like_javascript(mcp: McpClient, a: float, b: float, total: str) -> None:
    result: Any = await mcp.call_tool("get-sum", {"a": a, "b": b})
    assert text(result).endswith(f" is {total}.")


@pytest.mark.parametrize(
    ("arguments", "problems"),
    [
        ({"a": 1}, ["received undefined at b"]),
        ({"b": "2", "a": 1}, ["received string at b"]),
        ({"a": None, "b": True}, ["received null at a", "received boolean at b"]),
        ({}, ["received undefined at a", "received undefined at b"]),
    ],
)
async def test_sum_refuses_non_numbers(
    mcp: McpClient, arguments: dict[str, Any], problems: list[str]
) -> None:
    result: Any = await mcp.call_tool("get-sum", arguments)
    assert result["isError"] is True
    assert text(result) == f"{VALIDATION}get-sum: " + "\n".join(
        f"Invalid input: expected number, {problem}" for problem in problems
    )


# -- annotated messages, image, env, structured content ------------------------------------------


@pytest.mark.parametrize("message_type", ["error", "success", "debug"])
@pytest.mark.parametrize("include_image", [True, False])
async def test_annotated_messages(mcp: McpClient, message_type: str, include_image: bool) -> None:
    result: Any = await mcp.call_tool(
        "get-annotated-message", {"messageType": message_type, "includeImage": include_image}
    )
    content = result["content"]
    assert content[0]["type"] == "text"
    assert "priority" in content[0]["annotations"]
    assert len(content) == (2 if include_image else 1)
    if include_image:
        assert content[1]["mimeType"] == "image/png"
        assert content[1]["annotations"] == {"audience": ["user"], "priority": 0.5}


@pytest.mark.parametrize(
    "arguments",
    [
        {"messageType": "warning"},
        {"messageType": "error", "includeImage": 1},
        {"includeImage": True},
    ],
)
async def test_annotated_message_refusals(mcp: McpClient, arguments: dict[str, Any]) -> None:
    result: Any = await mcp.call_tool("get-annotated-message", arguments)
    assert result["isError"] is True
    assert text(result).startswith(f"{VALIDATION}get-annotated-message: ")


async def test_tiny_image_is_a_png(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("get-tiny-image", {"size": "ignored"})
    image = result["content"][1]
    assert base64.b64decode(image["data"])[:8] == b"\x89PNG\r\n\x1a\n"


async def test_env_is_pretty_json(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("get-env", {"verbose": True})
    assert text(result).startswith("{\n  ")


@pytest.mark.parametrize("city", ["Los Angeles", "Chicago", "New York"])
async def test_structured_content_matches_its_text(mcp: McpClient, city: str) -> None:
    result: Any = await mcp.call_tool(
        "get-structured-content", {"location": city, "units": "metric"}
    )
    structured = result["structuredContent"]
    assert set(structured) == {"temperature", "conditions", "humidity"}
    assert text(result).startswith('{"temperature":')


@pytest.mark.parametrize("location", ["chicago", "Paris", 42])
async def test_structured_content_refuses_other_cities(mcp: McpClient, location: Any) -> None:
    result: Any = await mcp.call_tool("get-structured-content", {"location": location})
    assert result["isError"] is True
    assert 'expected one of "New York"|"Chicago"|"Los Angeles" at location' in text(result)


# -- resource links and references ---------------------------------------------------------------


@pytest.mark.parametrize("count", [3, 4, 7, 9, 2.5, 1.0001])
async def test_resource_links_alternate_blob_and_text(mcp: McpClient, count: float) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": count})
    links = result["content"][1:]
    assert len(links) == int(count)
    for index, link in enumerate(links, start=1):
        kind = "blob" if index % 2 else "text"
        assert link["uri"] == f"demo://resource/dynamic/{kind}/{index}"
        assert link["type"] == "resource_link"


@pytest.mark.parametrize("count", [0.5, 10.5, -1, "four", None])
async def test_resource_links_count_bounds(mcp: McpClient, count: Any) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": count})
    assert result["isError"] is True


async def test_every_linked_resource_can_be_read(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": 4})
    for link in result["content"][1:]:
        read: Any = await mcp.request(
            "resources/read",
            {"uri": link["uri"]},
            compare=CompareOptions(
                policies=[
                    IgnorePolicy(path="contents[*].text", scope="body"),
                    IgnorePolicy(path="contents[*].blob", scope="body"),
                ]
            ),
        )
        assert read["contents"][0]["uri"] == link["uri"]


@pytest.mark.parametrize(("kind", "number"), [("Text", 4), ("Blob", 8), ("Text", 250), ("Blob", 3)])
async def test_resource_reference_embeds_the_resource(
    mcp: McpClient, kind: str, number: int
) -> None:
    result: Any = await mcp.call_tool(
        "get-resource-reference", {"resourceType": kind, "resourceId": number}, compare=CLOCK_TEXT
    )
    uri = f"demo://resource/dynamic/{kind.lower()}/{number}"
    assert text(result, 0) == f"Returning resource reference for Resource {number}:"
    assert text(result, 2) == f"You can access this resource using the URI: {uri}"
    resource = result["content"][1]["resource"]
    assert resource["uri"] == uri
    body = resource.get("text") or base64.b64decode(resource["blob"]).decode()
    stamp = re.fullmatch(rf"Resource {number}: This is a .* created at (.+)", body)
    assert stamp is not None
    assert_recent_local_time(stamp.group(1))


@pytest.mark.parametrize("number", [0, -3, 2.25])
async def test_resource_reference_needs_a_positive_integer(mcp: McpClient, number: float) -> None:
    result: Any = await mcp.call_tool("get-resource-reference", {"resourceId": number})
    assert result["isError"] is True
    assert text(result).startswith("Invalid resourceId: ")


async def test_resource_reference_refuses_wrong_types(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool(
        "get-resource-reference", {"resourceType": "blob", "resourceId": "1"}
    )
    assert text(result).count("\n") == 1  # both problems, one per line


# -- long-running operation ----------------------------------------------------------------------


async def test_long_running_operation_reports_its_parameters(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool(
        "trigger-long-running-operation", {"duration": 0.6, "steps": 3}
    )
    assert text(result) == "Long running operation completed. Duration: 0.6 seconds, Steps: 3."


async def test_long_running_operation_progress_notifications(mcp: McpClient) -> None:
    await mcp.request("ping")  # opens the session
    params: dict[str, Any] = {
        "name": "trigger-long-running-operation",
        "arguments": {"duration": 0.4, "steps": 4},
        "_meta": {"progressToken": "e2e-progress"},
    }
    call = await mcp.wire.call("tools/call", params)
    progress = [
        json.loads(data)["params"]
        for _, _, data, _ in call.sse_events
        if '"notifications/progress"' in data
    ]
    assert progress == [
        {"progress": step, "total": 4, "progressToken": "e2e-progress"} for step in (1, 2, 3, 4)
    ]


async def test_long_running_operation_refuses_text(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("trigger-long-running-operation", {"steps": "many"})
    assert text(result).endswith("expected number, received string at steps")


# -- unknown and capability-gated tools ----------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Echo", "get_sum", "trigger-sampling-request", "trigger-elicitation-request"]
)
async def test_tools_this_client_cannot_see(mcp: McpClient, name: str) -> None:
    result: Any = await mcp.call_tool(name, {})
    assert result == {
        "content": [{"type": "text", "text": f"MCP error -32602: Tool {name} not found"}],
        "isError": True,
    }
