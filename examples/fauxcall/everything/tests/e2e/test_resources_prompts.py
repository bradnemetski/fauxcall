"""Scenarios for resources, subscriptions, prompts, completions and logging levels."""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, Any, Final

import pytest
from fauxcall.model.fields import IgnorePolicy
from fauxcall.testing.client import CompareOptions
from test_tools import assert_recent_local_time

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

STATIC_DOCS: Final = (
    "architecture.md",
    "extension.md",
    "features.md",
    "how-it-works.md",
    "instructions.md",
    "startup.md",
    "structure.md",
)
CONTENT_CLOCK: Final = CompareOptions(
    policies=[
        IgnorePolicy(path="contents[*].text", scope="body"),
        IgnorePolicy(path="contents[*].blob", scope="body"),
    ]
)
PROMPT_CLOCK: Final = CompareOptions(
    policies=[
        IgnorePolicy(path="messages[*].content.resource.text", scope="body"),
        IgnorePolicy(path="messages[*].content.resource.blob", scope="body"),
    ]
)
"""Dynamic resources print the local time: asserted by the scenarios, not compared."""


def error_of(answer: Any) -> tuple[int, str]:
    return int(answer["code"]), str(answer["message"])


# -- listings ------------------------------------------------------------------------------------


async def test_listings(mcp: McpClient) -> None:
    tools: Any = await mcp.list_tools()
    assert [t["name"] for t in tools["tools"]][:3] == ["echo", "get-annotated-message", "get-env"]
    resources: Any = await mcp.request("resources/list", {})
    assert [r["name"] for r in resources["resources"]] == list(STATIC_DOCS)
    templates: Any = await mcp.request("resources/templates/list", {})
    assert len(templates["resourceTemplates"]) == 2
    prompts: Any = await mcp.request("prompts/list", {})
    assert [p["name"] for p in prompts["prompts"]] == [
        "simple-prompt",
        "args-prompt",
        "completable-prompt",
        "resource-prompt",
    ]


async def test_listings_ignore_an_unknown_cursor(mcp: McpClient) -> None:
    listed: Any = await mcp.request("prompts/list", {"cursor": "not-a-cursor"})
    assert len(listed["prompts"]) == 4


async def test_capable_clients_see_more_tools(mcp: McpClient, open_session: Any) -> None:
    capabilities = {"sampling": {}, "elicitation": {}, "roots": {"listChanged": True}}
    async with open_session(capabilities) as client:
        tools: Any = await client.list_tools()
    names = [t["name"] for t in tools["tools"]]
    assert {"trigger-sampling-request", "trigger-elicitation-request", "get-roots-list"} <= set(
        names
    )


# -- static and dynamic resources ----------------------------------------------------------------


@pytest.mark.parametrize("name", STATIC_DOCS)
async def test_static_documents(mcp: McpClient, name: str) -> None:
    read: Any = await mcp.request(
        "resources/read", {"uri": f"demo://resource/static/document/{name}"}
    )
    content = read["contents"][0]
    assert content["mimeType"] == "text/markdown"
    assert content["text"].startswith("# Everything Server")


@pytest.mark.parametrize(
    ("path", "number"),
    [("text/11", 11), ("text/0x20", 32), ("blob/1e2", 100), ("text/08", 8), ("blob/64", 64)],
)
async def test_dynamic_resources(mcp: McpClient, path: str, number: int) -> None:
    uri = f"demo://resource/dynamic/{path}"
    read: Any = await mcp.request("resources/read", {"uri": uri}, compare=CONTENT_CLOCK)
    content = read["contents"][0]
    assert content["uri"] == uri
    assert content["mimeType"] == "text/plain"
    body = content.get("text") or base64.b64decode(content["blob"]).decode()
    stamp = re.fullmatch(rf"Resource {number}: This is a .* created at (.+)", body)
    assert stamp is not None, body
    assert_recent_local_time(stamp.group(1))


async def test_uri_scheme_is_normalized(mcp: McpClient) -> None:
    read: Any = await mcp.request(
        "resources/read", {"uri": "Demo://resource/dynamic/text/5"}, compare=CONTENT_CLOCK
    )
    assert read["contents"][0]["uri"] == "demo://resource/dynamic/text/5"


@pytest.mark.parametrize("resource_id", ["0", "-5", "3.7", "xyz", "%2033"])
async def test_dynamic_resources_need_positive_integer_ids(
    mcp: McpClient, resource_id: str
) -> None:
    uri = f"demo://resource/dynamic/blob/{resource_id}"
    answer: Any = await mcp.request("resources/read", {"uri": uri})
    assert error_of(answer) == (-32603, f"Unknown resource: {uri}")


@pytest.mark.parametrize(
    "uri",
    [
        "demo://resource/static/document/README.md",
        "demo://resource/dynamic/text/5/",
        "demo://resource/dynamic/image/5",
        "demo://resource/session/never-created.gz",
        "file:///etc/hosts",
    ],
)
async def test_unknown_resources(mcp: McpClient, uri: str) -> None:
    answer: Any = await mcp.request("resources/read", {"uri": uri})
    assert error_of(answer) == (-32602, f"MCP error -32602: Resource {uri} not found")


async def test_resource_uri_must_be_a_url(mcp: McpClient) -> None:
    answer: Any = await mcp.request("resources/read", {"uri": "just words"})
    assert error_of(answer) == (-32603, "Invalid URL")
    missing: Any = await mcp.request("resources/read", {})
    assert missing["code"] == -32603
    assert '"expected": "string"' in missing["message"]


async def test_subscriptions_accept_any_uri(mcp: McpClient) -> None:
    for uri in ("demo://resource/dynamic/text/3", "demo://anything/at/all"):
        assert await mcp.request("resources/subscribe", {"uri": uri}) == {}
        assert await mcp.request("resources/unsubscribe", {"uri": uri}) == {}
    assert await mcp.request("resources/unsubscribe", {"uri": "demo://never"}) == {}
    refused: Any = await mcp.request("resources/subscribe", {"uri": ["demo://x"]})
    assert refused["code"] == -32603


# -- prompts -------------------------------------------------------------------------------------


async def test_simple_prompt(mcp: McpClient) -> None:
    prompt: Any = await mcp.request("prompts/get", {"name": "simple-prompt", "arguments": {}})
    assert prompt["messages"][0]["content"]["text"] == "This is a simple prompt without arguments."


@pytest.mark.parametrize(
    ("arguments", "question"),
    [
        ({"city": "Lyon"}, "What's weather in Lyon?"),
        ({"city": "Portland", "state": "Oregon"}, "What's weather in Portland, Oregon?"),
        ({"city": "Reno", "state": ""}, "What's weather in Reno?"),
    ],
)
async def test_args_prompt(mcp: McpClient, arguments: dict[str, Any], question: str) -> None:
    prompt: Any = await mcp.request("prompts/get", {"name": "args-prompt", "arguments": arguments})
    assert prompt["messages"] == [{"role": "user", "content": {"type": "text", "text": question}}]


async def test_prompt_argument_errors(mcp: McpClient) -> None:
    missing: Any = await mcp.request(
        "prompts/get", {"name": "args-prompt", "arguments": {"state": "CA"}}
    )
    assert error_of(missing) == (
        -32602,
        (
            "MCP error -32602: Invalid arguments for prompt args-prompt: "
            "Invalid input: expected string, received undefined at city"
        ),
    )
    typed: Any = await mcp.request(
        "prompts/get", {"name": "args-prompt", "arguments": {"city": True}}
    )
    assert typed["code"] == -32603
    unknown: Any = await mcp.request("prompts/get", {"name": "weather-prompt"})
    assert error_of(unknown) == (-32602, "MCP error -32602: Prompt weather-prompt not found")


async def test_completable_prompt(mcp: McpClient) -> None:
    prompt: Any = await mcp.request(
        "prompts/get",
        {"name": "completable-prompt", "arguments": {"department": "Support", "name": "Kim"}},
    )
    assert prompt["messages"][0]["content"]["text"] == (
        "Please promote Kim to the head of the Support team."
    )


@pytest.mark.parametrize(
    ("kind", "resource_id"), [("Text", "12"), ("Blob", "0x0f"), ("Text", " 6 ")]
)
async def test_resource_prompt(mcp: McpClient, kind: str, resource_id: str) -> None:
    prompt: Any = await mcp.request(
        "prompts/get",
        {"name": "resource-prompt", "arguments": {"resourceType": kind, "resourceId": resource_id}},
        compare=PROMPT_CLOCK,
    )
    number = int(resource_id, 0)
    intro, embedded = prompt["messages"]
    assert intro["content"]["text"].startswith(
        f"This prompt includes the {kind} resource with id: {number}."
    )
    assert embedded["content"]["resource"]["uri"] == (
        f"demo://resource/dynamic/{kind.lower()}/{number}"
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"resourceType": "Image", "resourceId": "1"},
            "Invalid resourceType: Image. Must be Text or Blob.",
        ),
        (
            {"resourceType": "Blob", "resourceId": "1.5"},
            "Invalid resourceId: 1.5. Must be a finite positive integer.",
        ),
        (
            {"resourceType": "Text", "resourceId": "one"},
            "Invalid resourceId: one. Must be a finite positive integer.",
        ),
    ],
)
async def test_resource_prompt_refusals(
    mcp: McpClient, arguments: dict[str, Any], message: str
) -> None:
    answer: Any = await mcp.request(
        "prompts/get", {"name": "resource-prompt", "arguments": arguments}
    )
    assert error_of(answer) == (-32603, message)


# -- completions ---------------------------------------------------------------------------------


def completion(values: list[str], *, total: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {"values": values}
    if total:
        body["total"] = len(values)
    body["hasMore"] = False
    return {"completion": body}


@pytest.mark.parametrize(
    ("value", "values"),
    [
        ("", ["Engineering", "Sales", "Marketing", "Support"]),
        ("S", ["Sales", "Support"]),
        ("Sup", ["Support"]),
        ("e", []),
        ("Engineering", ["Engineering"]),
    ],
)
async def test_department_completions(mcp: McpClient, value: str, values: list[str]) -> None:
    answer: Any = await mcp.request(
        "completion/complete",
        {
            "ref": {"type": "ref/prompt", "name": "completable-prompt"},
            "argument": {"name": "department", "value": value},
        },
    )
    assert answer == completion(values)


@pytest.mark.parametrize(
    ("department", "value", "values"),
    [
        ("Sales", "E", ["Eve"]),
        ("Marketing", "", ["Grace", "Henry", "Iris"]),
        ("Support", "L", ["Lee"]),
        ("Engineering", "C", ["Charlie"]),
        ("Finance", "", []),
    ],
)
async def test_team_member_completions_follow_the_department(
    mcp: McpClient, department: str, value: str, values: list[str]
) -> None:
    answer: Any = await mcp.request(
        "completion/complete",
        {
            "ref": {"type": "ref/prompt", "name": "completable-prompt"},
            "argument": {"name": "name", "value": value},
            "context": {"arguments": {"department": department}},
        },
    )
    assert answer == completion(values)


@pytest.mark.parametrize(
    ("ref", "argument", "expected"),
    [
        (
            {"type": "ref/prompt", "name": "resource-prompt"},
            {"name": "resourceType", "value": "B"},
            completion(["Blob"]),
        ),
        (
            {"type": "ref/prompt", "name": "resource-prompt"},
            {"name": "resourceId", "value": "42"},
            completion(["42"]),
        ),
        (
            {"type": "ref/resource", "uri": "demo://resource/dynamic/blob/{resourceId}"},
            {"name": "resourceId", "value": "9"},
            completion(["9"]),
        ),
        (
            {"type": "ref/resource", "uri": "demo://resource/dynamic/text/{resourceId}"},
            {"name": "resourceId", "value": "x"},
            completion([]),
        ),
        (
            {"type": "ref/prompt", "name": "args-prompt"},
            {"name": "state", "value": "Ne"},
            completion([], total=False),
        ),
    ],
)
async def test_other_completions(
    mcp: McpClient, ref: dict[str, Any], argument: dict[str, Any], expected: dict[str, Any]
) -> None:
    answer: Any = await mcp.request("completion/complete", {"ref": ref, "argument": argument})
    assert answer == expected


async def test_completion_errors(mcp: McpClient) -> None:
    unknown: Any = await mcp.request(
        "completion/complete",
        {"ref": {"type": "ref/prompt", "name": "ghost"}, "argument": {"name": "a", "value": ""}},
    )
    assert error_of(unknown) == (-32602, "MCP error -32602: Prompt ghost not found")
    template: Any = await mcp.request(
        "completion/complete",
        {
            "ref": {"type": "ref/resource", "uri": "demo://x/{y}"},
            "argument": {"name": "y", "value": ""},
        },
    )
    assert error_of(template) == (
        -32602,
        "MCP error -32602: Resource template demo://x/{y} not found",
    )
    malformed: Any = await mcp.request(
        "completion/complete", {"ref": {"type": "ref/tool", "name": "echo"}, "argument": {}}
    )
    assert malformed["code"] == -32603
    assert '"code": "invalid_union"' in malformed["message"]


# -- logging levels ------------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["notice", "error", "critical", "alert"])
async def test_logging_levels(mcp: McpClient, level: str) -> None:
    assert await mcp.request("logging/setLevel", {"level": level}) == {}


async def test_logging_level_must_be_a_syslog_level(mcp: McpClient) -> None:
    answer: Any = await mcp.request("logging/setLevel", {"level": "WARNING"})
    assert answer["code"] == -32603
    assert '"code": "invalid_value"' in answer["message"]


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (
            "demo://resource/static/document/./../document/startup.md",
            "demo://resource/static/document/startup.md",
        ),
        ("demo://resource/dynamic/blob/%2E%2E/text/12", "demo://resource/dynamic/text/12"),
    ],
)
async def test_dot_segments_are_resolved(mcp: McpClient, uri: str, expected: str) -> None:
    read: Any = await mcp.request("resources/read", {"uri": uri}, compare=CONTENT_CLOCK)
    assert read["contents"][0]["uri"] == expected


@pytest.mark.parametrize("uri", ["demo://bad host/x", "demo://resource:123456/dynamic/text/1"])
async def test_invalid_hosts_are_invalid_urls(mcp: McpClient, uri: str) -> None:
    answer: Any = await mcp.request("resources/read", {"uri": uri})
    assert error_of(answer) == (-32603, "Invalid URL")


async def test_template_ids_stop_at_commas(mcp: McpClient) -> None:
    answer: Any = await mcp.request("resources/read", {"uri": "demo://resource/dynamic/blob/3,4"})
    assert answer["code"] == -32602
