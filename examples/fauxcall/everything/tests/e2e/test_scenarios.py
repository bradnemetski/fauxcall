"""Listings and protocol behavior: what each kind of client is offered, and how the server refuses.

Each scenario runs against the fake (default), the real server (``--target=real``) or both with a
compliance report (``--target=parity``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject

BASE_TOOLS: Final = [
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


def names(listing: Any, key: str = "tools") -> list[str]:
    return [item["name"] for item in listing[key]]


async def test_a_plain_client_gets_the_base_tools_and_the_research_tool(mcp: McpClient) -> None:
    listing = await mcp.list_tools()
    assert names(listing) == [*BASE_TOOLS, "simulate-research-query"]


@pytest.mark.parametrize(
    ("capabilities", "extra"),
    [
        ({"sampling": {}}, ["trigger-sampling-request", "simulate-research-query"]),
        ({"roots": {"listChanged": True}}, ["get-roots-list", "simulate-research-query"]),
        ({"elicitation": {}}, ["trigger-elicitation-request", "simulate-research-query"]),
        (
            {"elicitation": {"form": {}, "url": {}}},
            ["trigger-elicitation-request", "trigger-url-elicitation", "simulate-research-query"],
        ),
        (
            {"sampling": {}, "tasks": {"requests": {"sampling": {"createMessage": {}}}}},
            [
                "trigger-sampling-request",
                "simulate-research-query",
                "trigger-sampling-request-async",
            ],
        ),
        (
            {"tasks": {"requests": {"sampling": {"createMessage": {}}}}},
            ["simulate-research-query"],
        ),
        (
            {
                "roots": {},
                "sampling": {},
                "elicitation": {"form": {}, "url": {}},
                "tasks": {
                    "requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}
                },
            },
            [
                "get-roots-list",
                "trigger-elicitation-request",
                "trigger-url-elicitation",
                "trigger-sampling-request",
                "simulate-research-query",
                "trigger-sampling-request-async",
                "trigger-elicitation-request-async",
            ],
        ),
    ],
)
async def test_capable_clients_get_more_tools(
    mcp: McpClient, capabilities: JsonObject, extra: list[str]
) -> None:
    await mcp.open(capabilities=capabilities)
    listing = await mcp.list_tools()
    assert names(listing) == [*BASE_TOOLS, *extra]


async def test_tool_definitions_carry_annotations_and_schemas(mcp: McpClient) -> None:
    listing: Any = await mcp.list_tools()
    tools = {tool["name"]: tool for tool in listing["tools"]}
    assert tools["echo"]["annotations"]["readOnlyHint"] is True
    assert tools["gzip-file-as-resource"]["annotations"]["openWorldHint"] is True
    assert tools["get-structured-content"]["outputSchema"]["required"] == [
        "temperature",
        "conditions",
        "humidity",
    ]
    assert tools["simulate-research-query"]["execution"] == {"taskSupport": "required"}


async def test_listing_cursors_are_ignored(mcp: McpClient) -> None:
    tools = await mcp.request("tools/list", {"cursor": "page-2"})
    prompts = await mcp.request("prompts/list", {"cursor": "later"})
    templates = await mcp.request("resources/templates/list", {"cursor": "abc"})
    assert names(tools) == [*BASE_TOOLS, "simulate-research-query"]
    assert len(names(prompts, "prompts")) == 4
    assert len(names(templates, "resourceTemplates")) == 2


async def test_resource_and_prompt_listings(mcp: McpClient) -> None:
    resources: Any = await mcp.request("resources/list", {})
    templates: Any = await mcp.request("resources/templates/list", {})
    prompts: Any = await mcp.request("prompts/list", {})
    assert [r["name"] for r in resources["resources"]] == [
        "architecture.md",
        "extension.md",
        "features.md",
        "how-it-works.md",
        "instructions.md",
        "startup.md",
        "structure.md",
    ]
    assert [t["uriTemplate"] for t in templates["resourceTemplates"]] == [
        "demo://resource/dynamic/text/{resourceId}",
        "demo://resource/dynamic/blob/{resourceId}",
    ]
    assert names(prompts, "prompts") == [
        "simple-prompt",
        "args-prompt",
        "completable-prompt",
        "resource-prompt",
    ]


async def test_ping_and_unknown_methods(mcp: McpClient) -> None:
    pong = await mcp.request("ping")
    unknown: Any = await mcp.request("resources/delete", {"uri": "x"})
    also: Any = await mcp.request("sampling/createMessage", {})
    assert pong == {}
    assert unknown == {"code": -32601, "message": "Method not found"}
    assert also["code"] == -32601


async def test_unknown_and_hidden_tools_are_not_found(mcp: McpClient) -> None:
    unknown: Any = await mcp.call_tool("no-such-tool", {})
    hidden: Any = await mcp.call_tool("trigger-elicitation-request", {})
    roots: Any = await mcp.call_tool("get-roots-list", {})
    assert unknown == {
        "content": [{"type": "text", "text": "MCP error -32602: Tool no-such-tool not found"}],
        "isError": True,
    }
    assert hidden["content"][0]["text"] == (
        "MCP error -32602: Tool trigger-elicitation-request not found"
    )
    assert roots["isError"] is True


async def test_malformed_tool_calls(mcp: McpClient) -> None:
    nameless: Any = await mcp.request("tools/call", {"arguments": {"message": "x"}})
    listed: Any = await mcp.request("tools/call", {"name": "get-sum", "arguments": [1, 2]})
    assert nameless["code"] == -32603
    assert '"name"' in nameless["message"]
    assert listed["code"] == -32603
    assert "expected record, received array" in listed["message"]


async def test_logging_levels(mcp: McpClient) -> None:
    for level in ("info", "critical", "alert"):
        assert await mcp.request("logging/setLevel", {"level": level}) == {}
    loud: Any = await mcp.request("logging/setLevel", {"level": "LOUD"})
    none: Any = await mcp.request("logging/setLevel", {})
    assert loud["code"] == none["code"] == -32603
    assert '"code": "invalid_value"' in loud["message"]


async def test_subscriptions_accept_any_uri(mcp: McpClient) -> None:
    assert await mcp.request("resources/subscribe", {"uri": "demo://resource/dynamic/blob/9"}) == {}
    assert await mcp.request("resources/subscribe", {"uri": "nothing://at/all"}) == {}
    assert (
        await mcp.request("resources/unsubscribe", {"uri": "demo://resource/dynamic/blob/9"}) == {}
    )
    assert await mcp.request("resources/unsubscribe", {"uri": "never-subscribed"}) == {}
    refused: Any = await mcp.request("resources/unsubscribe", {})
    assert refused["code"] == -32603
