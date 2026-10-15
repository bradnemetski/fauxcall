"""The plain tools: answers, JavaScript number spelling, zod refusals, links that resolve."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from scenario_support import LOCAL_TIME, PLAIN_TOOLS, STAMPED, dig, text, texts

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject


async def test_a_plain_client_sees_thirteen_tools(mcp: McpClient) -> None:
    listed = await mcp.list_tools()
    tools = dig(listed, "tools")
    assert isinstance(tools, list)
    assert [dig(t, "name") for t in tools] == PLAIN_TOOLS
    echo = tools[0]
    assert dig(echo, "annotations", "readOnlyHint") is True
    assert dig(echo, "execution", "taskSupport") == "forbidden"


@pytest.mark.parametrize(
    "message",
    ["Parity check from FauxCall", "", "ünïcødé ✓ 🚀", "tabs\tand\nnew lines", "  padded  "],
)
async def test_echo(mcp: McpClient, message: str) -> None:
    result = await mcp.call_tool("echo", {"message": message, "ignored": [1, 2]})
    assert text(result) == f"Echo: {message}"


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"message": 3.5}, "Invalid input: expected string, received number at message"),
        ({"text": "hi"}, "Invalid input: expected string, received undefined at message"),
        ({"message": None}, "Invalid input: expected string, received null at message"),
        ({"message": {"a": 1}}, "Invalid input: expected string, received object at message"),
    ],
)
async def test_echo_refuses_bad_arguments(
    mcp: McpClient, arguments: dict[str, object], problem: str
) -> None:
    result = await mcp.call_tool("echo", arguments)  # type: ignore[arg-type]
    assert dig(result, "isError") is True
    assert (
        text(result)
        == f"MCP error -32602: Input validation error: Invalid arguments for tool echo: {problem}"
    )


@pytest.mark.parametrize(
    ("a", "b", "spelled"),
    [
        (2, 40, "The sum of 2 and 40 is 42."),
        (-1.25, 0.25, "The sum of -1.25 and 0.25 is -1."),
        (1e-9, 5e-10, "The sum of 1e-9 and 5e-10 is 1.5000000000000002e-9."),
        (123.456, 7, "The sum of 123.456 and 7 is 130.45600000000002."),
        (1e300, 1e300, "The sum of 1e+300 and 1e+300 is 2e+300."),
        (9007199254740992, 1, "The sum of 9007199254740992 and 1 is 9007199254740992."),
        (0.7, 0.1, "The sum of 0.7 and 0.1 is 0.7999999999999999."),
        (5e21, 5e21, "The sum of 5e+21 and 5e+21 is 1e+22."),
    ],
)
async def test_sum_spells_numbers_like_javascript(
    mcp: McpClient, a: float, b: float, spelled: str
) -> None:
    assert text(await mcp.call_tool("get-sum", {"a": a, "b": b})) == spelled


async def test_sum_refuses_every_bad_argument_at_once(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-sum", {"a": "four", "b": True})
    assert text(result).endswith(
        "Invalid input: expected number, received string at a\n"
        "Invalid input: expected number, received boolean at b"
    )
    missing = await mcp.call_tool("get-sum", {"b": 1})
    assert text(missing).endswith("Invalid input: expected number, received undefined at a")


@pytest.mark.parametrize("message_type", ["error", "success", "debug"])
@pytest.mark.parametrize("include_image", [True, False])
async def test_annotated_messages(mcp: McpClient, message_type: str, include_image: bool) -> None:
    result = await mcp.call_tool(
        "get-annotated-message", {"messageType": message_type, "includeImage": include_image}
    )
    content = dig(result, "content")
    assert isinstance(content, list)
    assert len(content) == (2 if include_image else 1)
    assert list(dig(content[0], "annotations")) == ["audience", "priority"]  # type: ignore[arg-type]


async def test_annotated_message_refusals(mcp: McpClient) -> None:
    unknown = await mcp.call_tool("get-annotated-message", {"messageType": "warning"})
    assert text(unknown).endswith(
        'Invalid option: expected one of "error"|"success"|"debug" at messageType'
    )
    image = await mcp.call_tool(
        "get-annotated-message", {"messageType": "debug", "includeImage": 1}
    )
    assert text(image).endswith("Invalid input: expected boolean, received number at includeImage")


@pytest.mark.parametrize("city", ["Chicago", "Los Angeles", "New York"])
async def test_structured_weather(mcp: McpClient, city: str) -> None:
    result = await mcp.call_tool("get-structured-content", {"location": city, "units": "metric"})
    structured = dig(result, "structuredContent")
    assert isinstance(structured, dict)
    assert list(structured) == ["temperature", "conditions", "humidity"]
    assert text(result).startswith('{"temperature":')


async def test_structured_weather_only_for_three_cities(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-structured-content", {"location": "Paris"})
    assert dig(result, "isError") is True
    assert "Invalid option" in text(result)


async def test_image_and_environment_ignore_arguments(mcp: McpClient) -> None:
    image = await mcp.call_tool("get-tiny-image", {"format": "jpeg"})
    assert dig(image, "content", 1, "mimeType") == "image/png"
    assert text(image, 2) == "The image above is the MCP logo."
    environment = await mcp.call_tool("get-env", {"verbose": True})
    assert '"PORT"' in text(environment)


@pytest.mark.parametrize("count", [5, 7, 3.7, 10])
async def test_resource_links(mcp: McpClient, count: float) -> None:
    result = await mcp.call_tool("get-resource-links", {"count": count})
    links = [b for b in dig(result, "content") if dig(b, "type") == "resource_link"]  # type: ignore[union-attr]
    assert len(links) == int(count)
    assert all(dig(link, "mimeType") == "text/plain" for link in links)


@pytest.mark.parametrize("count", [0, 15, "two", -1, 0.5])
async def test_resource_links_refusals(mcp: McpClient, count: object) -> None:
    result = await mcp.call_tool("get-resource-links", {"count": count})  # type: ignore[dict-item]
    assert dig(result, "isError") is True


async def test_every_link_resolves(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-resource-links", {"count": 6})
    content = dig(result, "content")
    assert isinstance(content, list)
    for link in content[1:]:
        uri = dig(link, "uri")
        read = await mcp.request("resources/read", {"uri": uri}, compare=STAMPED)
        contents = dig(read, "contents", 0)
        assert dig(contents, "uri") == uri
        assert dig(contents, "mimeType") == "text/plain"


@pytest.mark.parametrize(
    ("arguments", "uri"),
    [
        ({"resourceType": "Text", "resourceId": 12}, "demo://resource/dynamic/text/12"),
        ({"resourceType": "Blob", "resourceId": 5}, "demo://resource/dynamic/blob/5"),
        ({"resourceId": 300}, "demo://resource/dynamic/text/300"),
        ({"resourceType": "Blob", "resourceId": 6.0}, "demo://resource/dynamic/blob/6"),
    ],
)
async def test_resource_reference_resolves(
    mcp: McpClient, arguments: dict[str, object], uri: str
) -> None:
    result = await mcp.call_tool("get-resource-reference", arguments, compare=STAMPED)  # type: ignore[arg-type]
    resource = dig(result, "content", 1, "resource")
    assert dig(resource, "uri") == uri
    assert texts(result)[-1] == f"You can access this resource using the URI: {uri}"
    read = await mcp.request("resources/read", {"uri": uri}, compare=STAMPED)
    if "text" in resource:  # type: ignore[operator]
        assert LOCAL_TIME.search(str(dig(read, "contents", 0, "text")))


@pytest.mark.parametrize("resource_id", [0, -2, 2.5])
async def test_resource_reference_refuses_ids(mcp: McpClient, resource_id: float) -> None:
    result = await mcp.call_tool(
        "get-resource-reference", {"resourceType": "Text", "resourceId": resource_id}
    )
    assert dig(result, "isError") is True
    assert text(result).startswith("Invalid resourceId: ")


async def test_resource_reference_refuses_types(mcp: McpClient) -> None:
    result = await mcp.call_tool("get-resource-reference", {"resourceType": "Audio"})
    assert text(result).endswith('Invalid option: expected one of "Text"|"Blob" at resourceType')


async def test_unknown_tools_and_tasks_for_plain_tools(mcp: McpClient) -> None:
    unknown = await mcp.call_tool("get-weather", {"city": "Rome"})
    assert text(unknown) == "MCP error -32602: Tool get-weather not found"
    as_task = await mcp.request(
        "tools/call", {"name": "get-sum", "arguments": {"a": 1, "b": 1}, "task": {"ttl": 1000}}
    )
    assert dig(as_task, "code") == -32602


@pytest.mark.parametrize(
    ("arguments", "summary"),
    [
        ({"duration": 0.6, "steps": 3}, "Duration: 0.6 seconds, Steps: 3."),
        ({"duration": 0, "steps": 4}, "Duration: 0 seconds, Steps: 4."),
        ({"duration": 2, "steps": 0}, "Duration: 2 seconds, Steps: 0."),
        ({"duration": 0.3, "steps": 1.5}, "Duration: 0.3 seconds, Steps: 1.5."),
    ],
)
async def test_long_running_operation(mcp: McpClient, arguments: JsonObject, summary: str) -> None:
    result = await mcp.request(
        "tools/call",
        {
            "name": "trigger-long-running-operation",
            "arguments": arguments,
            "_meta": {"progressToken": "p"},
        },
    )
    assert text(result) == f"Long running operation completed. {summary}"


async def test_long_running_operation_refusal(mcp: McpClient) -> None:
    result = await mcp.call_tool("trigger-long-running-operation", {"duration": "1s"})
    assert text(result).endswith("Invalid input: expected number, received string at duration")
