"""Resources, prompts and completions: documents, dynamic templates, arguments and completers."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest
from policies import STAMPED_PROMPT, STAMPED_READ

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject


async def test_every_listed_document_reads_back(mcp: McpClient) -> None:
    listing: Any = await mcp.request("resources/list", {})
    for resource in listing["resources"]:
        read: Any = await mcp.request("resources/read", {"uri": resource["uri"]})
        content = read["contents"][0]
        assert content["uri"] == resource["uri"]
        assert content["mimeType"] == resource["mimeType"]
        assert content["text"].startswith("# Everything Server")


@pytest.mark.parametrize("resource_id", ["3", "17", "250", "07", "1e1"])
async def test_dynamic_text_resources(mcp: McpClient, resource_id: str) -> None:
    uri = f"demo://resource/dynamic/text/{resource_id}"
    read: Any = await mcp.request("resources/read", {"uri": uri}, compare=STAMPED_READ)
    assert read["contents"][0]["uri"] == uri
    assert read["contents"][0]["text"].startswith("Resource ")


@pytest.mark.parametrize("resource_id", ["2", "64", "999"])
async def test_dynamic_blob_resources(mcp: McpClient, resource_id: str) -> None:
    uri = f"demo://resource/dynamic/blob/{resource_id}"
    read: Any = await mcp.request("resources/read", {"uri": uri}, compare=STAMPED_READ)
    sentence = base64.b64decode(read["contents"][0]["blob"]).decode()
    assert sentence.startswith(f"Resource {resource_id}: This is a base64 blob created at ")


@pytest.mark.parametrize(
    "uri",
    [
        "demo://resource/dynamic/text/zero",
        "demo://resource/dynamic/text/-7",
        "demo://resource/dynamic/blob/3.5",
        "demo://resource/dynamic/blob/0",
        "demo://resource/dynamic/text/1/2",
        "demo://resource/dynamic/blob/",
        "demo://resource/static/document/README.md",
        "demo://resource/static/document/features",
        "demo://resource/session/anything.gz",
        "demo://elsewhere/x",
        "https://example.com/doc.md",
    ],
)
async def test_unreadable_resources(mcp: McpClient, uri: str) -> None:
    read: Any = await mcp.request("resources/read", {"uri": uri})
    assert read["code"] in {-32602, -32603}


async def test_resource_read_refusals(mcp: McpClient) -> None:
    missing: Any = await mcp.request("resources/read", {})
    number: Any = await mcp.request("resources/read", {"uri": 42})
    assert missing["code"] == number["code"] == -32603


@pytest.mark.parametrize(
    ("name", "arguments", "text"),
    [
        ("simple-prompt", {}, "This is a simple prompt without arguments."),
        ("simple-prompt", {"anything": "goes"}, "This is a simple prompt without arguments."),
        ("args-prompt", {"city": "Lisbon"}, "What's weather in Lisbon?"),
        ("args-prompt", {"city": "Portland", "state": "OR"}, "What's weather in Portland, OR?"),
        ("args-prompt", {"city": "Nome", "state": ""}, "What's weather in Nome?"),
        ("args-prompt", {"city": "Zürich", "extra": "x"}, "What's weather in Zürich?"),
        (
            "completable-prompt",
            {"department": "Marketing", "name": "Iris"},
            "Please promote Iris to the head of the Marketing team.",
        ),
        (
            "completable-prompt",
            {"department": "Legal", "name": "Nobody"},
            "Please promote Nobody to the head of the Legal team.",
        ),
    ],
)
async def test_prompts(mcp: McpClient, name: str, arguments: JsonObject, text: str) -> None:
    prompt: Any = await mcp.request("prompts/get", {"name": name, "arguments": arguments})
    assert prompt["messages"][0]["content"]["text"] == text


@pytest.mark.parametrize(
    "arguments",
    [
        {"resourceType": "Text", "resourceId": "8"},
        {"resourceType": "Blob", "resourceId": "21"},
        {"resourceType": "Text", "resourceId": " 4 "},
    ],
)
async def test_resource_prompt(mcp: McpClient, arguments: JsonObject) -> None:
    prompt: Any = await mcp.request(
        "prompts/get", {"name": "resource-prompt", "arguments": arguments}, compare=STAMPED_PROMPT
    )
    assert prompt["messages"][1]["content"]["type"] == "resource"


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("args-prompt", {"state": "WA"}, -32602),
        ("completable-prompt", {"name": "Bob"}, -32602),
        ("resource-prompt", {"resourceId": "1"}, -32602),
        ("resource-prompt", {"resourceType": "Video", "resourceId": "1"}, -32603),
        ("resource-prompt", {"resourceType": "Blob", "resourceId": "-4"}, -32603),
        ("resource-prompt", {"resourceType": "Text", "resourceId": "two"}, -32603),
        ("args-prompt", {"city": True}, -32603),
        ("no-such-prompt", {}, -32602),
    ],
)
async def test_prompt_refusals(mcp: McpClient, name: str, arguments: JsonObject, code: int) -> None:
    refused: Any = await mcp.request("prompts/get", {"name": name, "arguments": arguments})
    assert refused["code"] == code


def ref_prompt(name: str) -> JsonObject:
    return {"type": "ref/prompt", "name": name}


@pytest.mark.parametrize(
    ("params", "values"),
    [
        (
            {
                "ref": ref_prompt("completable-prompt"),
                "argument": {"name": "department", "value": "Su"},
            },
            ["Support"],
        ),
        (
            {
                "ref": ref_prompt("completable-prompt"),
                "argument": {"name": "department", "value": "En"},
            },
            ["Engineering"],
        ),
        (
            {
                "ref": ref_prompt("completable-prompt"),
                "argument": {"name": "name", "value": "G"},
                "context": {"arguments": {"department": "Marketing"}},
            },
            ["Grace"],
        ),
        (
            {
                "ref": ref_prompt("completable-prompt"),
                "argument": {"name": "name", "value": "F"},
                "context": {"arguments": {"department": "Sales"}},
            },
            ["Frank"],
        ),
        (
            {
                "ref": ref_prompt("completable-prompt"),
                "argument": {"name": "name", "value": "a"},
                "context": {"arguments": {"department": "Engineering"}},
            },
            [],
        ),
        (
            {
                "ref": ref_prompt("resource-prompt"),
                "argument": {"name": "resourceType", "value": "T"},
            },
            ["Text"],
        ),
        (
            {
                "ref": ref_prompt("resource-prompt"),
                "argument": {"name": "resourceId", "value": "12"},
            },
            ["12"],
        ),
        (
            {
                "ref": ref_prompt("resource-prompt"),
                "argument": {"name": "resourceId", "value": "1.5"},
            },
            [],
        ),
        ({"ref": ref_prompt("args-prompt"), "argument": {"name": "state", "value": "W"}}, []),
        (
            {
                "ref": {"type": "ref/resource", "uri": "demo://resource/dynamic/blob/{resourceId}"},
                "argument": {"name": "resourceId", "value": "88"},
            },
            ["88"],
        ),
        (
            {
                "ref": {"type": "ref/resource", "uri": "demo://resource/dynamic/text/{resourceId}"},
                "argument": {"name": "resourceId", "value": "-1"},
            },
            [],
        ),
        (
            {
                "ref": {
                    "type": "ref/resource",
                    "uri": "demo://resource/static/document/startup.md",
                },
                "argument": {"name": "anything", "value": ""},
            },
            [],
        ),
    ],
)
async def test_completions(mcp: McpClient, params: JsonObject, values: list[str]) -> None:
    completed: Any = await mcp.request("completion/complete", params)
    assert completed["completion"]["values"] == values


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"ref": ref_prompt("missing"), "argument": {"name": "a", "value": ""}}, -32602),
        (
            {
                "ref": {"type": "ref/resource", "uri": "demo://x/{y}"},
                "argument": {"name": "y", "value": ""},
            },
            -32602,
        ),
        ({"ref": {"type": "ref/prompt"}, "argument": {"name": "a", "value": ""}}, -32603),
        ({"ref": ref_prompt("args-prompt"), "argument": {"name": "city"}}, -32603),
        ({"argument": {"name": "a", "value": ""}}, -32603),
    ],
)
async def test_completion_refusals(mcp: McpClient, params: JsonObject, code: int) -> None:
    refused: Any = await mcp.request("completion/complete", params)
    assert refused["code"] == code
