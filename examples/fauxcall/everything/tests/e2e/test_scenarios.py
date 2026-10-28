"""Listings and the simple read-only tools of the Everything server.

New inputs throughout (the recordings mostly used others), so the fake's computed answers are
compared, not its replays: argument checks worded as zod words them, JavaScript number spelling,
annotations, structured content and the capability-dependent tool lists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scenario_support import text_of

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

BASE_TOOLS = [
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


def names(listing: object) -> list[str]:
    assert isinstance(listing, dict)
    return [tool["name"] for tool in listing["tools"]]


# -- listings, per client capability ----------------------------------------------------------


async def test_tools_for_a_plain_client(mcp: McpClient) -> None:
    assert names(await mcp.list_tools()) == [*BASE_TOOLS, "simulate-research-query"]


async def test_tools_for_a_sampling_client(mcp: McpClient) -> None:
    await mcp.open(capabilities={"sampling": {}})
    listed = names(await mcp.list_tools())
    assert listed[12:] == ["trigger-sampling-request", "simulate-research-query"]


async def test_tools_for_an_elicitation_client(mcp: McpClient) -> None:
    await mcp.open(capabilities={"elicitation": {"form": {}}})
    assert names(await mcp.list_tools())[12:] == [
        "trigger-elicitation-request",
        "simulate-research-query",
    ]


async def test_tools_for_a_url_elicitation_client(mcp: McpClient) -> None:
    await mcp.open(capabilities={"elicitation": {"url": {}}})
    assert names(await mcp.list_tools())[12:] == [
        "trigger-elicitation-request",
        "trigger-url-elicitation",
        "simulate-research-query",
    ]


async def test_tools_for_a_roots_client(mcp: McpClient) -> None:
    await mcp.open(capabilities={"roots": {}})
    assert names(await mcp.list_tools())[12:] == ["get-roots-list", "simulate-research-query"]


async def test_tools_for_a_client_that_can_do_everything(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={
            "roots": {"listChanged": True},
            "sampling": {},
            "elicitation": {"form": {}, "url": {}},
            "tasks": {
                "requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}
            },
        }
    )
    assert names(await mcp.list_tools())[12:] == [
        "get-roots-list",
        "trigger-elicitation-request",
        "trigger-url-elicitation",
        "trigger-sampling-request",
        "simulate-research-query",
        "trigger-sampling-request-async",
        "trigger-elicitation-request-async",
    ]


async def test_async_elicitation_needs_the_task_capability_too(mcp: McpClient) -> None:
    await mcp.open(capabilities={"elicitation": {}, "tasks": {"requests": {}}})
    assert "trigger-elicitation-request-async" not in names(await mcp.list_tools())


async def test_resource_prompt_and_template_listings(mcp: McpClient) -> None:
    resources: Any = await mcp.request("resources/list", {})
    assert isinstance(resources, dict)
    assert [r["name"] for r in resources["resources"]] == [
        "architecture.md",
        "extension.md",
        "features.md",
        "how-it-works.md",
        "instructions.md",
        "startup.md",
        "structure.md",
    ]
    templates: Any = await mcp.request("resources/templates/list", {})
    assert isinstance(templates, dict)
    assert len(templates["resourceTemplates"]) == 2
    prompts: Any = await mcp.request("prompts/list", {})
    assert isinstance(prompts, dict)
    assert [p["name"] for p in prompts["prompts"]] == [
        "simple-prompt",
        "args-prompt",
        "completable-prompt",
        "resource-prompt",
    ]
    assert await mcp.request("ping", {}) == {}


async def test_a_capability_tool_without_the_capability_is_unknown(mcp: McpClient) -> None:
    refused: Any = await mcp.call_tool("trigger-elicitation-request", {})
    assert refused == {
        "content": [
            {"type": "text", "text": "MCP error -32602: Tool trigger-elicitation-request not found"}
        ],
        "isError": True,
    }
    unknown: Any = await mcp.call_tool("no-such-tool", {"x": 1})
    assert isinstance(unknown, dict)
    assert unknown["isError"] is True


# -- echo, sum, annotations, structured content ------------------------------------------------


async def test_echo_variations(mcp: McpClient) -> None:
    assert text_of(await mcp.call_tool("echo", {"message": "parity check 42"})) == (
        "Echo: parity check 42"
    )
    assert text_of(await mcp.call_tool("echo", {"message": "ünïcödé ✓ 😀"})) == (
        "Echo: ünïcödé ✓ 😀"
    )
    await mcp.call_tool("echo", {"message": "tabs\tand\nnewlines", "ignored": [1, 2]})
    await mcp.call_tool("echo", {"message": " "})


async def test_echo_refuses_bad_arguments(mcp: McpClient) -> None:
    refused: Any = await mcp.call_tool("echo", {"message": 3.5})
    assert text_of(refused).endswith("expected string, received number at message")
    await mcp.call_tool("echo", {"msg": "typo"})
    await mcp.call_tool("echo", {"message": {"nested": True}})
    await mcp.call_tool("echo", {"message": False})


async def test_sum_spells_numbers_like_javascript(mcp: McpClient) -> None:
    assert text_of(await mcp.call_tool("get-sum", {"a": 17, "b": 25})) == (
        "The sum of 17 and 25 is 42."
    )
    assert text_of(await mcp.call_tool("get-sum", {"a": -1.25, "b": 0.75})) == (
        "The sum of -1.25 and 0.75 is -0.5."
    )
    await mcp.call_tool("get-sum", {"a": 1e-9, "b": 2e-9})
    await mcp.call_tool("get-sum", {"a": 1152921504606846976, "b": 5})
    await mcp.call_tool("get-sum", {"a": 0.5, "b": 3e21})
    await mcp.call_tool("get-sum", {"a": 0.7, "b": 0.1})
    await mcp.call_tool("get-sum", {"a": 123456.789, "b": -0.000001})


async def test_sum_refuses_bad_arguments(mcp: McpClient) -> None:
    await mcp.call_tool("get-sum", {"a": [1], "b": 2})
    await mcp.call_tool("get-sum", {"a": 1, "b": "two"})
    both: Any = await mcp.call_tool("get-sum", {"a": None, "b": {}})
    assert "\n" in text_of(both)


async def test_annotated_messages(mcp: McpClient) -> None:
    for kind in ("debug", "error", "success"):
        result: Any = await mcp.call_tool("get-annotated-message", {"messageType": kind})
        assert isinstance(result, dict)
        assert len(result["content"]) == 1
    with_image: Any = await mcp.call_tool(
        "get-annotated-message", {"messageType": "success", "includeImage": True}
    )
    assert isinstance(with_image, dict)
    assert with_image["content"][1]["mimeType"] == "image/png"
    await mcp.call_tool("get-annotated-message", {"messageType": "info"})
    await mcp.call_tool("get-annotated-message", {"messageType": "error", "includeImage": "no"})
    await mcp.call_tool("get-annotated-message", {"includeImage": True})


async def test_structured_weather(mcp: McpClient) -> None:
    for city in ("Los Angeles", "New York"):
        result: Any = await mcp.call_tool("get-structured-content", {"location": city, "unit": "C"})
        assert isinstance(result, dict)
        assert set(result["structuredContent"]) == {"temperature", "conditions", "humidity"}
    await mcp.call_tool("get-structured-content", {"location": "Seattle"})
    await mcp.call_tool("get-structured-content", {"location": None})
    await mcp.call_tool("get-structured-content", {})


async def test_image_and_environment(mcp: McpClient) -> None:
    image: Any = await mcp.call_tool("get-tiny-image", {"anything": "ignored"})
    assert isinstance(image, dict)
    assert [block["type"] for block in image["content"]] == ["text", "image", "text"]
    environment: Any = await mcp.call_tool("get-env", {})
    assert '"PORT"' in text_of(environment)
