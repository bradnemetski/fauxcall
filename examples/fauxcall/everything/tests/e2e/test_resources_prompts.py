"""Resources (documents, templates), subscriptions and log levels, prompts and completions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from everything_support import made_now

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject

DOCS = (
    "architecture.md",
    "extension.md",
    "features.md",
    "how-it-works.md",
    "instructions.md",
    "startup.md",
    "structure.md",
)
NOW_TEXT = made_now("$.contents[0].text", "$.contents[0].blob")


async def test_every_listed_document_reads(mcp: McpClient) -> None:
    listing: Any = await mcp.request("resources/list", {})
    assert isinstance(listing, dict)
    for resource in listing["resources"]:
        read: Any = await mcp.request("resources/read", {"uri": resource["uri"]})
        assert isinstance(read, dict)
        assert read["contents"][0]["mimeType"] == resource["mimeType"]
    assert len(listing["resources"]) == len(DOCS)


@pytest.mark.parametrize("kind", ["text", "blob"])
@pytest.mark.parametrize("resource_id", ["3", "12", "77", "4096", "08", "2e1", "1.0"])
async def test_dynamic_resources(mcp: McpClient, kind: str, resource_id: str) -> None:
    uri = f"demo://resource/dynamic/{kind}/{resource_id}"
    read: Any = await mcp.request("resources/read", {"uri": uri}, compare=NOW_TEXT)
    assert isinstance(read, dict)
    assert read["contents"][0]["uri"] == uri


@pytest.mark.parametrize(
    "uri",
    [
        "demo://resource/dynamic/text/0",
        "demo://resource/dynamic/blob/-1",
        "demo://resource/dynamic/text/two",
        "demo://resource/dynamic/blob/3.5",
        "demo://resource/dynamic/text/Infinity",
        "demo://resource/dynamic/text/",
        "demo://resource/dynamic/text/1/2",
        "demo://resource/static/document/missing.md",
        "demo://resource/static/document/Features.md",
        "demo://resource/session/never-made.gz",
        "demo://elsewhere/thing",
        "https://example.com/not-a-resource",
        "no scheme here",
        "demo://resource/static/document/a b.md",
    ],
)
async def test_unreadable_resources(mcp: McpClient, uri: str) -> None:
    error: Any = await mcp.request("resources/read", {"uri": uri})
    assert isinstance(error, dict)
    assert error["code"] in {-32602, -32603}


async def test_read_without_a_uri(mcp: McpClient) -> None:
    await mcp.request("resources/read", {})
    await mcp.request("resources/read", {"uri": 7})
    await mcp.request("resources/read", {"uri": None})


async def test_resource_links_resolve(mcp: McpClient) -> None:
    links: Any = await mcp.call_tool("get-resource-links", {"count": 4})
    assert isinstance(links, dict)
    for block in links["content"][1:]:
        read: Any = await mcp.request("resources/read", {"uri": block["uri"]}, compare=NOW_TEXT)
        assert isinstance(read, dict)
        assert read["contents"][0]["uri"] == block["uri"]


async def test_subscribe_and_unsubscribe_are_acknowledged(mcp: McpClient) -> None:
    await mcp.listen()
    assert await mcp.request("resources/subscribe", {"uri": "demo://resource/dynamic/text/8"}) == {}
    assert await mcp.request("resources/subscribe", {"uri": "demo://anything/at/all"}) == {}
    await mcp.heard("notifications/message", count=2, within=5)
    assert (
        await mcp.request("resources/unsubscribe", {"uri": "demo://resource/dynamic/text/8"}) == {}
    )
    await mcp.heard("notifications/message", count=1, within=5)
    await mcp.request("resources/subscribe", {})


async def test_log_level_filters_acknowledgements(mcp: McpClient) -> None:
    await mcp.listen()
    assert await mcp.request("logging/setLevel", {"level": "notice"}) == {}
    await mcp.request("resources/subscribe", {"uri": "demo://resource/dynamic/blob/5"})
    assert await mcp.request("logging/setLevel", {"level": "info"}) == {}
    await mcp.request("resources/subscribe", {"uri": "demo://resource/dynamic/blob/6"})
    await mcp.heard("notifications/message", count=1, within=5)


@pytest.mark.parametrize("level", ["debug", "warning", "emergency", "alert"])
async def test_set_level(mcp: McpClient, level: str) -> None:
    assert await mcp.request("logging/setLevel", {"level": level}) == {}


async def test_set_level_refuses(mcp: McpClient) -> None:
    await mcp.request("logging/setLevel", {"level": "trace"})
    await mcp.request("logging/setLevel", {"level": "INFO"})
    await mcp.request("logging/setLevel", {})


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("simple-prompt", None),
        ("simple-prompt", {"ignored": "yes"}),
        ("args-prompt", {"city": "Lisbon"}),
        ("args-prompt", {"city": "Springfield", "state": "Illinois"}),
        ("args-prompt", {"city": "Nowhere", "state": ""}),
        ("args-prompt", {"city": "", "state": "Utah"}),
        ("completable-prompt", {"department": "Support", "name": "Kim"}),
        ("completable-prompt", {"department": "Legal", "name": "Saul", "extra": "x"}),
    ],
)
async def test_prompts(mcp: McpClient, name: str, arguments: JsonObject | None) -> None:
    params: JsonObject = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    result: Any = await mcp.request("prompts/get", params)
    assert isinstance(result, dict)
    assert result["messages"][0]["role"] == "user"


@pytest.mark.parametrize(
    "arguments",
    [{"resourceType": "Text", "resourceId": "9"}, {"resourceType": "Blob", "resourceId": "0x1A"}],
)
async def test_resource_prompt(mcp: McpClient, arguments: JsonObject) -> None:
    result: Any = await mcp.request(
        "prompts/get",
        {"name": "resource-prompt", "arguments": arguments},
        compare=made_now(
            "$.messages[1].content.resource.text", "$.messages[1].content.resource.blob"
        ),
    )
    assert isinstance(result, dict)
    assert result["messages"][1]["content"]["type"] == "resource"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("args-prompt", {}),
        ("args-prompt", {"state": "Maine"}),
        ("args-prompt", {"city": 5}),
        ("completable-prompt", {}),
        ("completable-prompt", {"name": "Bob"}),
        ("resource-prompt", {"resourceType": "Video", "resourceId": "1"}),
        ("resource-prompt", {"resourceType": "Text", "resourceId": "-3"}),
        ("resource-prompt", {"resourceType": "Blob", "resourceId": "one"}),
        ("resource-prompt", {"resourceId": "2"}),
        ("missing-prompt", {}),
    ],
)
async def test_prompts_refuse(mcp: McpClient, name: str, arguments: JsonObject) -> None:
    error: Any = await mcp.request("prompts/get", {"name": name, "arguments": arguments})
    assert isinstance(error, dict)
    assert "code" in error


async def test_prompt_without_a_name(mcp: McpClient) -> None:
    await mcp.request("prompts/get", {})
    await mcp.request("prompts/get", {"name": 3})


@pytest.mark.parametrize(
    ("argument", "value", "context"),
    [
        ("department", "", None),
        ("department", "M", None),
        ("department", "Su", None),
        ("department", "Z", None),
        ("name", "", {"department": "Sales"}),
        ("name", "F", {"department": "Sales"}),
        ("name", "", {"department": "Marketing"}),
        ("name", "C", {"department": "Engineering"}),
        ("name", "", {"department": "Unknown"}),
        ("name", "J", None),
        ("unknown", "", None),
    ],
)
async def test_complete_team_prompt(
    mcp: McpClient, argument: str, value: str, context: JsonObject | None
) -> None:
    params: JsonObject = {
        "ref": {"type": "ref/prompt", "name": "completable-prompt"},
        "argument": {"name": argument, "value": value},
    }
    if context is not None:
        params["context"] = {"arguments": context}
    result: Any = await mcp.request("completion/complete", params)
    assert isinstance(result, dict)
    assert "values" in result["completion"]


@pytest.mark.parametrize(
    ("ref", "argument", "value"),
    [
        ({"type": "ref/prompt", "name": "resource-prompt"}, "resourceType", "T"),
        ({"type": "ref/prompt", "name": "resource-prompt"}, "resourceType", "x"),
        ({"type": "ref/prompt", "name": "resource-prompt"}, "resourceId", "42"),
        ({"type": "ref/prompt", "name": "resource-prompt"}, "resourceId", "4.2"),
        ({"type": "ref/prompt", "name": "args-prompt"}, "city", "B"),
        ({"type": "ref/prompt", "name": "simple-prompt"}, "x", ""),
        (
            {"type": "ref/resource", "uri": "demo://resource/dynamic/text/{resourceId}"},
            "resourceId",
            "7",
        ),
        (
            {"type": "ref/resource", "uri": "demo://resource/dynamic/blob/{resourceId}"},
            "resourceId",
            "-7",
        ),
        (
            {"type": "ref/resource", "uri": "demo://resource/dynamic/blob/{resourceId}"},
            "other",
            "1",
        ),
        ({"type": "ref/resource", "uri": "demo://resource/static/document/startup.md"}, "a", "b"),
    ],
)
async def test_completions(mcp: McpClient, ref: JsonObject, argument: str, value: str) -> None:
    result: Any = await mcp.request(
        "completion/complete", {"ref": ref, "argument": {"name": argument, "value": value}}
    )
    assert isinstance(result, dict)
    assert "completion" in result


async def test_completions_refuse(mcp: McpClient) -> None:
    await mcp.request(
        "completion/complete",
        {"ref": {"type": "ref/prompt", "name": "nope"}, "argument": {"name": "a", "value": ""}},
    )
    await mcp.request(
        "completion/complete",
        {
            "ref": {"type": "ref/resource", "uri": "demo://x/{y}"},
            "argument": {"name": "y", "value": ""},
        },
    )
    await mcp.request(
        "completion/complete", {"ref": {"type": "ref/prompt", "name": "completable-prompt"}}
    )
