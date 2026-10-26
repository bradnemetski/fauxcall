"""Listings, capability-dependent tools, and how the server refuses malformed requests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from everything_support import ALL_CAPABILITIES

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


def _names(listing: object) -> list[str]:
    assert isinstance(listing, dict)
    tools = listing["tools"]
    assert isinstance(tools, list)
    return [str(tool["name"]) for tool in tools if isinstance(tool, dict)]


async def test_tools_listed_to_a_plain_client(mcp: McpClient) -> None:
    names = _names(await mcp.list_tools())
    assert names == [*BASE_TOOLS, "simulate-research-query"]


async def test_tools_listed_to_a_client_that_can_do_everything(mcp: McpClient) -> None:
    await mcp.open(capabilities=ALL_CAPABILITIES)
    names = _names(await mcp.list_tools())
    assert names[: len(BASE_TOOLS)] == BASE_TOOLS
    assert names[len(BASE_TOOLS) :] == [
        "get-roots-list",
        "trigger-elicitation-request",
        "trigger-url-elicitation",
        "trigger-sampling-request",
        "simulate-research-query",
        "trigger-sampling-request-async",
        "trigger-elicitation-request-async",
    ]


async def test_sampling_only_client_gets_the_sampling_tool(mcp: McpClient) -> None:
    await mcp.open(capabilities={"sampling": {}})
    names = _names(await mcp.list_tools())
    assert names[len(BASE_TOOLS) :] == ["trigger-sampling-request", "simulate-research-query"]


async def test_roots_and_form_elicitation_client(mcp: McpClient) -> None:
    await mcp.open(capabilities={"roots": {}, "elicitation": {}})
    names = _names(await mcp.list_tools())
    assert names[len(BASE_TOOLS) :] == [
        "get-roots-list",
        "trigger-elicitation-request",
        "simulate-research-query",
    ]


async def test_url_elicitation_client(mcp: McpClient) -> None:
    await mcp.open(capabilities={"elicitation": {"url": {}}})
    names = _names(await mcp.list_tools())
    assert "trigger-url-elicitation" in names
    assert "trigger-sampling-request" not in names


async def test_async_tools_need_both_capabilities(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {}, "tasks": {"requests": {"elicitation": {"create": {}}}}}
    )
    names = _names(await mcp.list_tools())
    assert names[-1] == "trigger-elicitation-request-async"
    assert "trigger-sampling-request-async" not in names


async def test_tasks_capability_alone_unlocks_nothing(mcp: McpClient) -> None:
    await mcp.open(capabilities={"tasks": {"requests": {"sampling": {"createMessage": {}}}}})
    names = _names(await mcp.list_tools())
    assert names == [*BASE_TOOLS, "simulate-research-query"]


async def test_tool_list_ignores_an_unknown_cursor(mcp: McpClient) -> None:
    listing: Any = await mcp.request("tools/list", {"cursor": "not-a-real-cursor"})
    assert len(_names(listing)) == len(BASE_TOOLS) + 1


async def test_resource_and_prompt_listings(mcp: McpClient) -> None:
    resources: Any = await mcp.request("resources/list", {})
    assert isinstance(resources, dict)
    assert len(resources["resources"]) == 7
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


async def test_listings_with_cursors(mcp: McpClient) -> None:
    await mcp.request("resources/list", {"cursor": "abc"})
    await mcp.request("prompts/list", {"cursor": "xyz"})
    await mcp.request("resources/templates/list", {"cursor": "nope"})


async def test_ping(mcp: McpClient) -> None:
    assert await mcp.request("ping") == {}
    assert await mcp.request("ping", {}) == {}


async def test_unknown_methods(mcp: McpClient) -> None:
    error: Any = await mcp.request("does/not-exist", {"x": 1})
    assert isinstance(error, dict)
    assert error["code"] == -32601
    await mcp.request("sampling/createMessage", {"messages": [], "maxTokens": 5})
    await mcp.request("elicitation/create", {"message": "hi"})


async def test_unknown_tool(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("no-such-tool", {"a": 1})
    assert isinstance(result, dict)
    assert result["isError"] is True


async def test_roots_tool_unknown_without_the_capability(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("get-roots-list", {})
    assert isinstance(result, dict)
    assert result["isError"] is True


async def test_malformed_tool_calls(mcp: McpClient) -> None:
    await mcp.request("tools/call", {"name": 42, "arguments": {}})
    await mcp.request("tools/call", {"name": True})
    await mcp.request("tools/call", {"arguments": {"message": "no name"}})
    await mcp.request("tools/call", {"name": "echo", "arguments": ["message", "x"]})
    await mcp.request("tools/call", {"name": "echo", "arguments": "message"})
    await mcp.request("tools/call", {"name": "get-sum", "arguments": 3})


async def test_task_augmented_call_of_a_plain_tool(mcp: McpClient) -> None:
    error: Any = await mcp.request(
        "tools/call", {"name": "get-sum", "arguments": {"a": 1, "b": 2}, "task": {"ttl": 5000}}
    )
    assert isinstance(error, dict)
    assert error["code"] == -32602
    await mcp.request("tools/call", {"name": "echo", "arguments": {}, "task": {}})
