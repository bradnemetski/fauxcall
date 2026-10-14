"""Resources, templates, prompts, completions, logging and subscriptions."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest
from scenario_support import LOCAL_TIME, STAMPED_PROMPT, STAMPED_READ

if TYPE_CHECKING:
    from fauxcall.testing import McpClient


async def test_listings(mcp: McpClient) -> None:
    resources: Any = await mcp.request("resources/list", {}, label="resources/list")
    assert len(resources["resources"]) == 7
    await mcp.request("resources/templates/list", {}, label="resources/templates/list")
    await mcp.request("prompts/list", {}, label="prompts/list")


async def test_every_listed_document_reads(mcp: McpClient) -> None:
    resources: Any = await mcp.request("resources/list", {}, label="resources/list")
    for resource in resources["resources"]:
        read: Any = await mcp.request(
            "resources/read", {"uri": resource["uri"]}, label=f"read {resource['name']}"
        )
        assert read["contents"][0]["mimeType"] == resource["mimeType"]


@pytest.mark.parametrize(
    "uri",
    [
        "demo://resource/dynamic/text/77",
        "demo://resource/dynamic/blob/77",
        "demo://resource/dynamic/text/2e1",
        "demo://resource/dynamic/blob/0X1f",
        "Demo://resource/dynamic/blob/9",
    ],
)
async def test_dynamic_resources(mcp: McpClient, uri: str) -> None:
    read: Any = await mcp.request("resources/read", {"uri": uri}, label=uri, compare=STAMPED_READ)
    content = read["contents"][0]
    body = content.get("text") or base64.b64decode(content["blob"]).decode()
    assert LOCAL_TIME.match(body.rsplit(" created at ", 1)[1])


@pytest.mark.parametrize(
    "uri",
    [
        "demo://resource/dynamic/text/-7",
        "demo://resource/dynamic/blob/2.5",
        "demo://resource/dynamic/text/NaN",
        "demo://resource/dynamic/text/Infinity",
        "demo://resource/dynamic/text/1/extra",
        "demo://resource/static/document/README.md",
        "demo://resource/session/anything.gz",
        "file:///etc/passwd",
        "not a uri",
    ],
)
async def test_unreadable_resources(mcp: McpClient, uri: str) -> None:
    await mcp.request("resources/read", {"uri": uri}, label=uri)


async def test_read_without_uri(mcp: McpClient) -> None:
    await mcp.request("resources/read", {"url": "demo://x"}, label="resources/read without uri")


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("simple-prompt", {}),
        ("args-prompt", {"city": "Lisbon"}),
        ("args-prompt", {"city": "Portland", "state": "Oregon"}),
        ("args-prompt", {"city": "Paris", "country": "France"}),
        ("args-prompt", {}),
        ("args-prompt", {"city": ["x"]}),
        ("completable-prompt", {"department": "Support", "name": "Kim"}),
        ("completable-prompt", {"department": "Legal", "name": "Ann"}),
        ("completable-prompt", {"name": "Bob"}),
        ("resource-prompt", {"resourceType": "Text", "resourceId": "x7"}),
        ("resource-prompt", {"resourceType": "Image", "resourceId": "3"}),
        ("resource-prompt", {"resourceType": "Blob", "resourceId": "0.5"}),
        ("resource-prompt", {"resourceId": "3"}),
        ("missing-prompt", {}),
    ],
)
async def test_prompts(mcp: McpClient, name: str, arguments: dict[str, Any]) -> None:
    await mcp.request(
        "prompts/get", {"name": name, "arguments": arguments}, label=f"prompts/get {name}"
    )


@pytest.mark.parametrize(("kind", "resource_id"), [("Text", "21"), ("Blob", "8"), ("Text", "2e0")])
async def test_resource_prompt_embeds_the_resource(
    mcp: McpClient, kind: str, resource_id: str
) -> None:
    got: Any = await mcp.request(
        "prompts/get",
        {"name": "resource-prompt", "arguments": {"resourceType": kind, "resourceId": resource_id}},
        label=f"resource-prompt {kind}",
        compare=STAMPED_PROMPT,
    )
    embedded = got["messages"][1]["content"]["resource"]
    assert embedded["uri"].startswith(f"demo://resource/dynamic/{kind.lower()}/")
    assert embedded["mimeType"] == "text/plain"


def _complete(ref: dict[str, Any], name: str, value: str, context: Any = None) -> dict[str, Any]:
    params: dict[str, Any] = {"ref": ref, "argument": {"name": name, "value": value}}
    if context is not None:
        params["context"] = {"arguments": context}
    return params


TEAM = {"type": "ref/prompt", "name": "completable-prompt"}
EMBED = {"type": "ref/prompt", "name": "resource-prompt"}
TEXT_TEMPLATE = {"type": "ref/resource", "uri": "demo://resource/dynamic/text/{resourceId}"}
BLOB_TEMPLATE = {"type": "ref/resource", "uri": "demo://resource/dynamic/blob/{resourceId}"}


@pytest.mark.parametrize(
    "params",
    [
        _complete(TEAM, "department", "S"),
        _complete(TEAM, "department", "Eng"),
        _complete(TEAM, "department", "e"),
        _complete(TEAM, "name", "", {"department": "Marketing"}),
        _complete(TEAM, "name", "H", {"department": "Marketing"}),
        _complete(TEAM, "name", "L", {"department": "Support"}),
        _complete(TEAM, "name", "", {}),
        _complete(EMBED, "resourceType", "Bl"),
        _complete(EMBED, "resourceId", "42"),
        _complete(EMBED, "resourceId", "-4"),
        _complete(TEXT_TEMPLATE, "resourceId", "33"),
        _complete(BLOB_TEMPLATE, "resourceId", "7"),
        _complete(BLOB_TEMPLATE, "resourceId", "0.1"),
        _complete({"type": "ref/prompt", "name": "args-prompt"}, "state", "Ca"),
        _complete({"type": "ref/prompt", "name": "unknown"}, "x", ""),
        _complete(
            {"type": "ref/resource", "uri": "demo://resource/static/document/startup.md"}, "x", ""
        ),
        _complete({"type": "ref/resource", "uri": "demo://elsewhere/{x}"}, "x", ""),
    ],
)
async def test_completions(mcp: McpClient, params: dict[str, Any]) -> None:
    await mcp.request("completion/complete", params, label="completion/complete")


@pytest.mark.parametrize("level", ["info", "warning", "critical", "alert", "Debug", "fatal"])
async def test_logging_levels(mcp: McpClient, level: str) -> None:
    await mcp.request("logging/setLevel", {"level": level}, label=f"logging/setLevel {level}")


async def test_subscriptions(mcp: McpClient) -> None:
    uri = "demo://resource/dynamic/blob/5"
    assert await mcp.request("resources/subscribe", {"uri": uri}, label="subscribe") == {}
    assert await mcp.request("resources/subscribe", {"uri": uri}, label="subscribe again") == {}
    assert await mcp.request("resources/unsubscribe", {"uri": uri}, label="unsubscribe") == {}
    await mcp.request("resources/unsubscribe", {}, label="unsubscribe without uri")
