"""Resources, templates, prompts, completions and the small protocol methods."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest
from scenario_support import LOCAL_TIME, STAMPED, dig

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject, JsonValue

DOCUMENTS = [
    "architecture.md",
    "extension.md",
    "features.md",
    "how-it-works.md",
    "instructions.md",
    "startup.md",
    "structure.md",
]


async def test_listings(mcp: McpClient) -> None:
    resources = await mcp.request("resources/list", {})
    listed = dig(resources, "resources")
    assert isinstance(listed, list)
    assert [dig(r, "name") for r in listed] == DOCUMENTS
    templates = await mcp.request("resources/templates/list", {})
    uris = [dig(t, "uriTemplate") for t in dig(templates, "resourceTemplates")]  # type: ignore[union-attr]
    assert uris == [
        "demo://resource/dynamic/text/{resourceId}",
        "demo://resource/dynamic/blob/{resourceId}",
    ]
    prompts = await mcp.request("prompts/list", {})
    names = [dig(p, "name") for p in dig(prompts, "prompts")]  # type: ignore[union-attr]
    assert names == ["simple-prompt", "args-prompt", "completable-prompt", "resource-prompt"]


async def test_instructions_document_is_the_server_instructions(mcp: McpClient) -> None:
    read = await mcp.request(
        "resources/read", {"uri": "demo://resource/static/document/instructions.md"}
    )
    assert str(dig(read, "contents", 0, "text")).startswith(
        "# Everything Server – Server Instructions"  # noqa: RUF001 - the document's own dash
    )


@pytest.mark.parametrize(
    "uri",
    [
        "DEMO://resource/static/document/architecture.md",
        "Demo://resource/static/document/how-it-works.md",
        "demo://resource/static/document/./extension.md",
    ],
)
async def test_static_documents_by_equivalent_uris(mcp: McpClient, uri: str) -> None:
    read = await mcp.request("resources/read", {"uri": uri})
    contents = dig(read, "contents", 0)
    assert str(dig(contents, "uri")).startswith("demo://resource/static/document/")
    assert dig(contents, "mimeType") == "text/markdown"


@pytest.mark.parametrize(
    ("uri", "resource_id"),
    [
        ("demo://resource/dynamic/text/42", "42"),
        ("demo://resource/dynamic/blob/9", "9"),
        ("demo://resource/dynamic/text/1e2", "100"),
        ("demo://resource/dynamic/blob/0x1F", "31"),
        ("demo://resource/dynamic/text/0b101", "5"),
        ("demo://resource/dynamic/blob/3.0", "3"),
        ("DEMO://resource/dynamic/text/8", "8"),
        ("demo://resource/dynamic/text/../blob/4", "4"),
    ],
)
async def test_dynamic_resources(mcp: McpClient, uri: str, resource_id: str) -> None:
    read = await mcp.request("resources/read", {"uri": uri}, compare=STAMPED)
    contents = dig(read, "contents", 0)
    assert dig(contents, "mimeType") == "text/plain"
    if "text" in contents:  # type: ignore[operator]
        written = str(dig(contents, "text"))
    else:
        written = base64.b64decode(str(dig(contents, "blob"))).decode()
    assert written.startswith(f"Resource {resource_id}: This is a ")
    assert LOCAL_TIME.search(written)


@pytest.mark.parametrize(
    "uri",
    [
        "demo://resource/dynamic/text/0",
        "demo://resource/dynamic/blob/-5",
        "demo://resource/dynamic/text/seven",
        "demo://resource/dynamic/text/2.5",
        "demo://resource/dynamic/blob/NaN",
        "demo://resource/dynamic/text/4?fresh=1",
    ],
)
async def test_dynamic_resources_need_positive_integers(mcp: McpClient, uri: str) -> None:
    refused = await mcp.request("resources/read", {"uri": uri})
    assert refused == {"code": -32603, "message": f"Unknown resource: {uri}"}


@pytest.mark.parametrize(
    ("uri", "named"),
    [
        ("demo://resource/dynamic/text/1,2", "demo://resource/dynamic/text/1,2"),
        ("demo://resource/dynamic/blob/", "demo://resource/dynamic/blob/"),
        ("demo://resource/dynamic/text/3/4", "demo://resource/dynamic/text/3/4"),
        ("demo://resource/static/document/README.md", "demo://resource/static/document/README.md"),
        ("HTTPS://Example.org", "https://example.org/"),
        ("mailto:someone@example.com", "mailto:someone@example.com"),
    ],
)
async def test_unknown_resources(mcp: McpClient, uri: str, named: str) -> None:
    refused = await mcp.request("resources/read", {"uri": uri})
    assert refused == {"code": -32602, "message": f"MCP error -32602: Resource {named} not found"}


@pytest.mark.parametrize("uri", ["not a uri", "", "//resource/x"])
async def test_resources_need_urls(mcp: McpClient, uri: str) -> None:
    assert await mcp.request("resources/read", {"uri": uri}) == {
        "code": -32603,
        "message": "Invalid URL",
    }


async def test_resources_read_needs_a_uri(mcp: McpClient) -> None:
    refused = await mcp.request("resources/read", {"url": "demo://resource/dynamic/text/1"})
    assert dig(refused, "code") == -32603
    assert '"path": [\n      "params",\n      "uri"\n    ]' in str(dig(refused, "message"))


@pytest.mark.parametrize(
    ("name", "arguments", "first_text"),
    [
        ("simple-prompt", {}, "This is a simple prompt without arguments."),
        ("args-prompt", {"city": "Lisbon"}, "What's weather in Lisbon?"),
        (
            "args-prompt",
            {"city": "Springfield", "state": "Illinois"},
            "What's weather in Springfield, Illinois?",
        ),
        (
            "completable-prompt",
            {"department": "Support", "name": "Kim"},
            "Please promote Kim to the head of the Support team.",
        ),
        (
            "completable-prompt",
            {"department": "Research", "name": "Ada"},
            "Please promote Ada to the head of the Research team.",
        ),
    ],
)
async def test_prompts(mcp: McpClient, name: str, arguments: JsonObject, first_text: str) -> None:
    got = await mcp.request("prompts/get", {"name": name, "arguments": arguments})
    assert dig(got, "messages", 0, "content", "text") == first_text


@pytest.mark.parametrize(("kind", "resource_id"), [("Text", "8"), ("Blob", "21"), ("Text", "0x10")])
async def test_resource_prompt_embeds_a_dynamic_resource(
    mcp: McpClient, kind: str, resource_id: str
) -> None:
    got = await mcp.request(
        "prompts/get",
        {"name": "resource-prompt", "arguments": {"resourceType": kind, "resourceId": resource_id}},
        compare=STAMPED,
    )
    resource = dig(got, "messages", 1, "content", "resource")
    assert str(dig(resource, "uri")).startswith(f"demo://resource/dynamic/{kind.lower()}/")


@pytest.mark.parametrize(
    ("name", "arguments", "code", "message"),
    [
        (
            "args-prompt",
            {"state": "Ohio"},
            -32602,
            "MCP error -32602: Invalid arguments for prompt args-prompt: Invalid input: expected string, received undefined at city",
        ),
        (
            "completable-prompt",
            {},
            -32602,
            "MCP error -32602: Invalid arguments for prompt completable-prompt: Invalid input: expected string, received undefined at department\nInvalid input: expected string, received undefined at name",
        ),
        (
            "resource-prompt",
            {"resourceType": "Video", "resourceId": "1"},
            -32603,
            "Invalid resourceType: Video. Must be Text or Blob.",
        ),
        (
            "resource-prompt",
            {"resourceType": "Blob", "resourceId": "-3"},
            -32603,
            "Invalid resourceId: -3. Must be a finite positive integer.",
        ),
        ("weather-prompt", {}, -32602, "MCP error -32602: Prompt weather-prompt not found"),
    ],
)
async def test_prompt_refusals(
    mcp: McpClient, name: str, arguments: JsonObject, code: int, message: str
) -> None:
    refused = await mcp.request("prompts/get", {"name": name, "arguments": arguments})
    assert refused == {"code": code, "message": message}


async def test_prompt_arguments_must_be_strings(mcp: McpClient) -> None:
    refused = await mcp.request("prompts/get", {"name": "args-prompt", "arguments": {"city": True}})
    assert dig(refused, "code") == -32603


def _complete(
    ref: JsonValue, name: str, value: str, context: JsonObject | None = None
) -> JsonObject:
    params: JsonObject = {"ref": ref, "argument": {"name": name, "value": value}}
    if context is not None:
        params["context"] = {"arguments": context}
    return params


TEAM: JsonObject = {"type": "ref/prompt", "name": "completable-prompt"}
RESOURCE_PROMPT: JsonObject = {"type": "ref/prompt", "name": "resource-prompt"}


@pytest.mark.parametrize(
    ("params", "values"),
    [
        (_complete(TEAM, "department", "S"), ["Sales", "Support"]),
        (_complete(TEAM, "department", "Mark"), ["Marketing"]),
        (_complete(TEAM, "name", "", {"department": "Marketing"}), ["Grace", "Henry", "Iris"]),
        (_complete(TEAM, "name", "K", {"department": "Support"}), ["Kim"]),
        (_complete(TEAM, "name", "A", {"department": "Legal"}), []),
        (_complete(RESOURCE_PROMPT, "resourceType", "T"), ["Text"]),
        (_complete(RESOURCE_PROMPT, "resourceType", ""), ["Text", "Blob"]),
        (_complete(RESOURCE_PROMPT, "resourceId", "77"), ["77"]),
        (_complete(RESOURCE_PROMPT, "resourceId", "0"), []),
        (
            _complete(
                {"type": "ref/resource", "uri": "demo://resource/dynamic/blob/{resourceId}"},
                "resourceId",
                "5",
            ),
            ["5"],
        ),
        (
            _complete(
                {"type": "ref/resource", "uri": "demo://resource/dynamic/text/{resourceId}"},
                "resourceId",
                "x1",
            ),
            [],
        ),
    ],
)
async def test_completions(mcp: McpClient, params: JsonObject, values: list[str]) -> None:
    done = await mcp.request("completion/complete", params)
    assert dig(done, "completion", "values") == values
    assert dig(done, "completion", "total") == len(values)


@pytest.mark.parametrize(
    "params",
    [
        _complete({"type": "ref/prompt", "name": "args-prompt"}, "city", "Bo"),
        _complete({"type": "ref/prompt", "name": "simple-prompt"}, "x", ""),
        _complete(
            {"type": "ref/resource", "uri": "demo://resource/static/document/startup.md"}, "x", ""
        ),
    ],
)
async def test_nothing_to_complete(mcp: McpClient, params: JsonObject) -> None:
    assert await mcp.request("completion/complete", params) == {
        "completion": {"values": [], "hasMore": False}
    }


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            _complete({"type": "ref/prompt", "name": "team-prompt"}, "x", ""),
            "MCP error -32602: Prompt team-prompt not found",
        ),
        (
            _complete({"type": "ref/resource", "uri": "demo://resource/{id}"}, "id", "1"),
            "MCP error -32602: Resource template demo://resource/{id} not found",
        ),
    ],
)
async def test_completion_refusals(mcp: McpClient, params: JsonObject, message: str) -> None:
    assert await mcp.request("completion/complete", params) == {"code": -32602, "message": message}


async def test_completion_protocol_refusals(mcp: McpClient) -> None:
    wrong_ref = await mcp.request(
        "completion/complete", _complete({"type": "ref/tool", "name": "echo"}, "x", "")
    )
    assert dig(wrong_ref, "code") == -32603
    no_argument = await mcp.request("completion/complete", {"ref": TEAM})
    assert dig(no_argument, "code") == -32603


@pytest.mark.parametrize("level", ["warning", "critical", "info"])
async def test_logging_levels(mcp: McpClient, level: str) -> None:
    assert await mcp.request("logging/setLevel", {"level": level}) == {}


async def test_logging_level_refusal_and_ping(mcp: McpClient) -> None:
    refused = await mcp.request("logging/setLevel", {"level": "verbose"})
    assert dig(refused, "code") == -32603
    assert await mcp.request("ping", {}) == {}


@pytest.mark.parametrize(
    "uri",
    [
        "demo://resource/static/document/structure.md",
        "demo://resource/dynamic/blob/2",
        "anything at all",
    ],
)
async def test_subscriptions_are_acknowledged(mcp: McpClient, uri: str) -> None:
    assert await mcp.request("resources/subscribe", {"uri": uri}) == {}
    assert await mcp.request("resources/unsubscribe", {"uri": uri}) == {}


async def test_tasks_methods_without_tasks(mcp: McpClient) -> None:
    assert await mcp.request("tasks/list", {}) == {"tasks": [], "_meta": {}}
    missing = "f" * 32
    assert await mcp.request("tasks/get", {"taskId": missing}) == {
        "code": -32602,
        "message": "MCP error -32602: Failed to retrieve task: Task not found",
    }
    assert await mcp.request("tasks/result", {"taskId": missing}) == {
        "code": -32602,
        "message": f"MCP error -32602: Task not found: {missing}",
    }
    assert await mcp.request("tasks/cancel", {"taskId": missing}) == {
        "code": -32602,
        "message": f"MCP error -32602: Task not found: {missing}",
    }
    no_id = await mcp.request("tasks/get", {})
    assert dig(no_id, "code") == -32603


async def test_unknown_method(mcp: McpClient) -> None:
    refused = await mcp.request("resources/delete", {"uri": "demo://x"})
    assert refused == {"code": -32601, "message": "Method not found"}
