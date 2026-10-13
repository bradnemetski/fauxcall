"""Listings, resources, prompts, completions, subscriptions and protocol errors."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import pytest
from everything_support import LOCAL_TIME, text_of, volatile

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

STATIC = [
    "architecture.md",
    "extension.md",
    "features.md",
    "how-it-works.md",
    "instructions.md",
    "startup.md",
    "structure.md",
]


async def test_listings(mcp: McpClient) -> None:
    tools: Any = await mcp.list_tools()
    names = [tool["name"] for tool in tools["tools"]]
    assert "echo" in names
    assert "trigger-sampling-request" not in names  # this client declares no sampling
    resources: Any = await mcp.request("resources/list", {}, label="resources/list")
    assert [r["name"] for r in resources["resources"]] == STATIC
    templates: Any = await mcp.request("resources/templates/list", {}, label="templates")
    assert len(templates["resourceTemplates"]) == 2
    prompts: Any = await mcp.request("prompts/list", {}, label="prompts/list")
    assert [p["name"] for p in prompts["prompts"]] == [
        "simple-prompt",
        "args-prompt",
        "completable-prompt",
        "resource-prompt",
    ]
    assert await mcp.request("ping", {}, label="ping") == {}


async def test_static_documents_link_to_each_other(mcp: McpClient) -> None:
    for name in STATIC:
        uri = f"demo://resource/static/document/{name}"
        read: Any = await mcp.request("resources/read", {"uri": uri})
        content = read["contents"][0]
        assert content["mimeType"] == "text/markdown"
        assert content["text"].startswith("# Everything Server")


@pytest.mark.parametrize(
    ("uri", "number"),
    [
        ("demo://resource/dynamic/text/7", "7"),
        ("demo://resource/dynamic/text/250", "250"),
        ("demo://resource/dynamic/text/0042", "42"),
        ("demo://resource/dynamic/blob/9", "9"),
        ("demo://resource/dynamic/blob/3e1", "30"),
    ],
)
async def test_dynamic_resources(mcp: McpClient, uri: str, number: str) -> None:
    read: Any = await mcp.request(
        "resources/read",
        {"uri": uri},
        compare=volatile("$.contents[0].text", "$.contents[0].blob"),
    )
    content = read["contents"][0]
    assert content["uri"] == uri
    assert content["mimeType"] == "text/plain"
    body = content.get("text") or base64.b64decode(content["blob"]).decode()
    assert body.startswith(f"Resource {number}: ")
    assert LOCAL_TIME.search(body)


@pytest.mark.parametrize(
    ("uri", "code"),
    [
        ("demo://resource/dynamic/text/0", -32603),
        ("demo://resource/dynamic/blob/seven", -32603),
        ("demo://resource/dynamic/text/2.5", -32603),
        ("demo://resource/dynamic/blob/", -32602),
        ("demo://resource/dynamic/text/3/4", -32602),
        ("demo://resource/static/document/README.md", -32602),
        ("https://example.com/resource", -32602),
        ("demo://resource/session/never-made.gz", -32602),
    ],
)
async def test_unreadable_resources(mcp: McpClient, uri: str, code: int) -> None:
    error: Any = await mcp.request("resources/read", {"uri": uri})
    assert error["code"] == code
    assert uri in error["message"]


async def test_read_without_a_uri(mcp: McpClient) -> None:
    error: Any = await mcp.request("resources/read", {"name": "x"})
    assert error["code"] == -32603
    assert json.loads(error["message"])[0]["path"] == ["params", "uri"]


async def test_subscriptions_and_levels(mcp: McpClient) -> None:
    assert await mcp.request("resources/subscribe", {"uri": "demo://resource/dynamic/text/5"}) == {}
    assert await mcp.request("resources/subscribe", {"uri": "anything://at/all"}) == {}
    assert (
        await mcp.request("resources/unsubscribe", {"uri": "demo://resource/dynamic/text/5"}) == {}
    )
    assert await mcp.request("resources/unsubscribe", {"uri": "demo://never/subscribed"}) == {}
    refused: Any = await mcp.request("resources/subscribe", {"url": "x"})
    assert refused["code"] == -32603
    for level in ("warning", "critical", "debug"):
        assert await mcp.request("logging/setLevel", {"level": level}) == {}
    bad: Any = await mcp.request("logging/setLevel", {"level": "verbose"})
    assert json.loads(bad["message"])[0]["values"][0] == "debug"


# -- prompts --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "text"),
    [
        ({"city": "Lisbon"}, "What's weather in Lisbon?"),
        ({"city": "Portland", "state": "Oregon"}, "What's weather in Portland, Oregon?"),
        ({"city": "Nome", "state": ""}, "What's weather in Nome?"),
    ],
)
async def test_args_prompt(mcp: McpClient, arguments: dict[str, Any], text: str) -> None:
    prompt: Any = await mcp.request("prompts/get", {"name": "args-prompt", "arguments": arguments})
    assert prompt["messages"] == [{"role": "user", "content": {"type": "text", "text": text}}]


@pytest.mark.parametrize(
    ("department", "name"), [("Support", "Kim"), ("Engineering", "Zoe"), ("Legal", "Sam")]
)
async def test_completable_prompt(mcp: McpClient, department: str, name: str) -> None:
    prompt: Any = await mcp.request(
        "prompts/get",
        {"name": "completable-prompt", "arguments": {"department": department, "name": name}},
    )
    assert prompt["messages"][0]["content"]["text"] == (
        f"Please promote {name} to the head of the {department} team."
    )


@pytest.mark.parametrize(("kind", "number"), [("Text", "12"), ("Blob", "2"), ("Text", "0x1F")])
async def test_resource_prompt(mcp: McpClient, kind: str, number: str) -> None:
    prompt: Any = await mcp.request(
        "prompts/get",
        {"name": "resource-prompt", "arguments": {"resourceType": kind, "resourceId": number}},
        compare=volatile(
            "$.messages[1].content.resource.text", "$.messages[1].content.resource.blob"
        ),
    )
    resource = prompt["messages"][1]["content"]["resource"]
    expected_id = str(int(number, 0))
    assert resource["uri"] == f"demo://resource/dynamic/{kind.lower()}/{expected_id}"
    assert f"with id: {expected_id}." in prompt["messages"][0]["content"]["text"]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"name": "args-prompt", "arguments": {"state": "Utah"}}, -32602),
        ({"name": "completable-prompt", "arguments": {"name": "Ann"}}, -32602),
        (
            {"name": "resource-prompt", "arguments": {"resourceType": "Video", "resourceId": "1"}},
            -32603,
        ),
        (
            {"name": "resource-prompt", "arguments": {"resourceType": "Blob", "resourceId": "-4"}},
            -32603,
        ),
        ({"name": "weather-prompt"}, -32602),
    ],
)
async def test_prompt_refusals(mcp: McpClient, params: dict[str, Any], code: int) -> None:
    error: Any = await mcp.request("prompts/get", params)
    assert error["code"] == code


# -- completions ----------------------------------------------------------------------------------

TEAM = {"type": "ref/prompt", "name": "completable-prompt"}
RESOURCE = {"type": "ref/prompt", "name": "resource-prompt"}
BLOB_TEMPLATE = {"type": "ref/resource", "uri": "demo://resource/dynamic/blob/{resourceId}"}


@pytest.mark.parametrize(
    ("ref", "argument", "value", "context", "values"),
    [
        (TEAM, "department", "Su", None, ["Support"]),
        (TEAM, "department", "", None, ["Engineering", "Sales", "Marketing", "Support"]),
        (TEAM, "name", "Ch", {"department": "Engineering"}, ["Charlie"]),
        (TEAM, "name", "", {"department": "Support"}, ["John", "Kim", "Lee"]),
        (TEAM, "name", "i", {"department": "Marketing"}, []),
        (RESOURCE, "resourceType", "", None, ["Text", "Blob"]),
        (RESOURCE, "resourceId", "12", None, ["12"]),
        (RESOURCE, "resourceId", "-3", None, []),
        (BLOB_TEMPLATE, "resourceId", "8", None, ["8"]),
        (BLOB_TEMPLATE, "resourceId", "", None, []),
    ],
)
async def test_completions(  # noqa: PLR0913, PLR0917 - one per parametrized column
    mcp: McpClient,
    ref: dict[str, str],
    argument: str,
    value: str,
    context: dict[str, str] | None,
    values: list[str],
) -> None:
    params: dict[str, Any] = {"ref": ref, "argument": {"name": argument, "value": value}}
    if context is not None:
        params["context"] = {"arguments": context}
    result: Any = await mcp.request("completion/complete", params)
    assert result["completion"]["values"] == values


async def test_completion_refusals(mcp: McpClient) -> None:
    unknown: Any = await mcp.request(
        "completion/complete",
        {"ref": {"type": "ref/prompt", "name": "team"}, "argument": {"name": "x", "value": ""}},
    )
    assert unknown["code"] == -32602
    template: Any = await mcp.request(
        "completion/complete",
        {
            "ref": {"type": "ref/resource", "uri": "demo://other/{id}"},
            "argument": {"name": "id", "value": "1"},
        },
    )
    assert template["code"] == -32602
    malformed: Any = await mcp.request(
        "completion/complete",
        {"ref": {"type": "ref/tool", "name": "echo"}, "argument": {"name": "x", "value": ""}},
    )
    assert malformed["code"] == -32603


# -- protocol errors ------------------------------------------------------------------------------


async def test_unknown_tool_and_method(mcp: McpClient) -> None:
    unknown: Any = await mcp.call_tool("no-such-tool", {})
    assert text_of(unknown) == "MCP error -32602: Tool no-such-tool not found"
    method: Any = await mcp.request("tools/explode", {}, label="unknown method")
    assert method == {"code": -32601, "message": "Method not found"}


async def test_malformed_tool_calls(mcp: McpClient) -> None:
    nameless: Any = await mcp.request(
        "tools/call", {"arguments": {}}, label="tools/call without name"
    )
    assert nameless["code"] == -32603
    listed: Any = await mcp.request(
        "tools/call", {"name": "get-sum", "arguments": [1, 2]}, label="tools/call:get-sum"
    )
    assert listed["code"] == -32603
    task: Any = await mcp.request(
        "tools/call",
        {"name": "echo", "arguments": {"message": "x"}, "task": "soon"},
        label="tools/call:echo",
    )
    assert task["code"] == -32603
    assert "received string" in task["message"]


async def test_tools_hidden_without_a_capability_answer_not_found(mcp: McpClient) -> None:
    result: Any = await mcp.request(
        "tools/call",
        {"name": "get-roots-list", "arguments": {}},
        label="tools/call:get-roots-list",
    )
    assert text_of(result) == "MCP error -32602: Tool get-roots-list not found"


@pytest.mark.parametrize(
    ("uri", "code"),
    [
        ("demo://resource/dynamic/blob/%32", -32603),
        ("demo://resource/dynamic/text/5?v=2", -32603),
        ("demo://resource/dynamic/text/5#top", -32603),
        ("demo://resource/dynamic/blob/3,4", -32602),
        ("just words", -32603),
        ("demo://resource/dynamic/text/2_000", -32603),
    ],
)
async def test_resource_uri_parsing(mcp: McpClient, uri: str, code: int) -> None:
    error: Any = await mcp.request("resources/read", {"uri": uri})
    assert error["code"] == code


@pytest.mark.parametrize(
    "uri", ["DEMO://resource/dynamic/blob/6", "demo://resource/dynamic/../dynamic/text/0x0A"]
)
async def test_resource_uris_are_normalized(mcp: McpClient, uri: str) -> None:
    read: Any = await mcp.request(
        "resources/read",
        {"uri": uri},
        compare=volatile("$.contents[0].text", "$.contents[0].blob"),
    )
    assert read["contents"][0]["uri"].startswith("demo://resource/dynamic/")


async def test_task_param_on_a_plain_tool(mcp: McpClient) -> None:
    error: Any = await mcp.request(
        "tools/call",
        {"name": "get-structured-content", "arguments": {"location": "Chicago"}, "task": {}},
        label="tools/call:get-structured-content",
    )
    assert error["code"] == -32602
    assert error["message"].startswith("MCP error -32602: Invalid task creation result: ")


async def test_request_schema_refusals(mcp: McpClient) -> None:
    prompt: Any = await mcp.request(
        "prompts/get", {"name": "completable-prompt", "arguments": {"department": 3, "name": "A"}}
    )
    assert json.loads(prompt["message"])[0]["path"] == ["params", "arguments", "department"]
    completion: Any = await mcp.request(
        "completion/complete",
        {
            "ref": {"type": "ref/prompt", "name": "args-prompt"},
            "argument": {"name": "city", "value": None},
        },
    )
    assert completion["code"] == -32603
    fixed: Any = await mcp.request(
        "completion/complete",
        {
            "ref": {"type": "ref/resource", "uri": "demo://resource/static/document/startup.md"},
            "argument": {"name": "x", "value": "a"},
        },
    )
    assert fixed == {"completion": {"values": [], "hasMore": False}}
    cursor: Any = await mcp.request("tasks/list", {"cursor": "not-a-task"}, label="tasks/list")
    assert cursor["message"] == "MCP error -32602: Failed to list tasks: Invalid cursor: not-a-task"


async def test_non_finite_numbers_are_refused(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("get-resource-links", {"count": 10**400})
    assert result["isError"] is True
    assert text_of(result).endswith("received Infinity at count")
