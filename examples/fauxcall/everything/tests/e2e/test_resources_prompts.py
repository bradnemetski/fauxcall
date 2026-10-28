"""Resources (documents, dynamic templates), prompts, completions and resource tools.

Dynamic resources embed the server's local time of day: those texts are checked by the
scenario (a time near now in America/New_York, on both sides) and left out of the value
comparison; every other value is compared.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from scenario_support import assert_recent_local_time, ignoring, text_of

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject

DOCS = "demo://resource/static/document"
LIVE_TEXT = ignoring("$.contents[*].text", "$.contents[*].blob")


async def read(mcp: McpClient, uri: str, *, live: bool = False) -> Any:
    return await mcp.request("resources/read", {"uri": uri}, compare=LIVE_TEXT if live else None)


async def test_every_document_reads_as_markdown(mcp: McpClient) -> None:
    listing: Any = await mcp.request("resources/list", {})
    assert isinstance(listing, dict)
    for resource in listing["resources"]:
        result: Any = await read(mcp, resource["uri"])
        assert isinstance(result, dict)
        content = result["contents"][0]
        assert content["mimeType"] == "text/markdown"
        assert content["text"].startswith("# Everything Server")


async def test_dynamic_text_and_blob_resources(mcp: McpClient) -> None:
    text: Any = await read(mcp, "demo://resource/dynamic/text/77", live=True)
    assert isinstance(text, dict)
    body = text["contents"][0]
    assert body["text"].startswith("Resource 77: This is a plaintext resource created at ")
    assert_recent_local_time(body["text"])
    blob: Any = await read(mcp, "demo://resource/dynamic/blob/8", live=True)
    assert isinstance(blob, dict)
    decoded = base64.b64decode(blob["contents"][0]["blob"]).decode()
    assert decoded.startswith("Resource 8: This is a base64 blob created at ")
    assert_recent_local_time(decoded)
    assert blob["contents"][0]["mimeType"] == "text/plain"


async def test_resource_ids_are_read_like_javascript_numbers(mcp: McpClient) -> None:
    padded: Any = await read(mcp, "demo://resource/dynamic/text/0042", live=True)
    assert isinstance(padded, dict)
    assert padded["contents"][0]["uri"] == "demo://resource/dynamic/text/0042"
    assert padded["contents"][0]["text"].startswith("Resource 42:")
    exponent: Any = await read(mcp, "demo://resource/dynamic/blob/2e1", live=True)
    assert isinstance(exponent, dict)
    hexadecimal: Any = await read(mcp, "demo://resource/dynamic/text/0x1F", live=True)
    assert isinstance(hexadecimal, dict)
    assert hexadecimal["contents"][0]["text"].startswith("Resource 31:")


async def test_uris_are_normalized_before_lookup(mcp: McpClient) -> None:
    upper: Any = await read(mcp, "DEMO://resource/static/document/startup.md")
    assert isinstance(upper, dict)
    assert upper["contents"][0]["uri"] == f"{DOCS}/startup.md"
    dotted: Any = await read(mcp, "demo://resource/dynamic/blob/../text/12", live=True)
    assert isinstance(dotted, dict)
    assert dotted["contents"][0]["uri"] == "demo://resource/dynamic/text/12"
    await read(mcp, f"{DOCS}/./features.md")


async def test_unknown_and_invalid_resources(mcp: McpClient) -> None:
    for uri in (
        "demo://resource/dynamic/text/0",
        "demo://resource/dynamic/blob/-7",
        "demo://resource/dynamic/text/2.5",
        "demo://resource/dynamic/blob/seven",
        "demo://resource/dynamic/text/9#fragment",
        f"{DOCS}/missing.md",
        "demo://resource/dynamic/audio/1",
        "demo://resource/session/nothing-here.gz",
        "file:///etc/hosts",
        "just words",
    ):
        error: Any = await read(mcp, uri)
        assert isinstance(error, dict)
        assert error["code"] in {-32602, -32603}, error
    missing: Any = await mcp.request("resources/read", {})
    assert isinstance(missing, dict)
    assert missing["code"] == -32603
    await mcp.request("resources/read", {"uri": 42})


async def test_resource_links_point_at_readable_resources(mcp: McpClient) -> None:
    links: Any = await mcp.call_tool("get-resource-links", {"count": 4})
    assert isinstance(links, dict)
    assert text_of(links) == "Here are 4 resource links to resources available in this server:"
    for link in links["content"][1:]:
        assert link["type"] == "resource_link"
        resolved: Any = await read(mcp, link["uri"], live=True)
        assert isinstance(resolved, dict)
        assert resolved["contents"][0]["uri"] == link["uri"]
    await mcp.call_tool("get-resource-links", {"count": 6})
    await mcp.call_tool("get-resource-links", {"count": 9.5})
    await mcp.call_tool("get-resource-links", {"count": 12})
    await mcp.call_tool("get-resource-links", {"count": 0.5})
    await mcp.call_tool("get-resource-links", {"count": True})


async def test_resource_reference_embeds_a_fresh_resource(mcp: McpClient) -> None:
    text: Any = await mcp.call_tool(
        "get-resource-reference", {"resourceType": "Text", "resourceId": 5}
    )
    assert isinstance(text, dict)
    embedded = text["content"][1]["resource"]
    assert embedded["uri"] == "demo://resource/dynamic/text/5"
    assert_recent_local_time(embedded["text"])
    blob: Any = await mcp.call_tool(
        "get-resource-reference", {"resourceType": "Blob", "resourceId": 12}
    )
    assert isinstance(blob, dict)
    assert blob["content"][1]["resource"]["uri"] == "demo://resource/dynamic/blob/12"
    exact = ignoring("$.content[1].resource.text")
    await mcp.call_tool("get-resource-reference", {"resourceId": 2}, compare=exact)
    refused: Any = await mcp.call_tool(
        "get-resource-reference", {"resourceType": "Text", "resourceId": 2.5}, compare=exact
    )
    assert text_of(refused) == "Invalid resourceId: 2.5. Must be a finite positive integer."
    await mcp.call_tool("get-resource-reference", {"resourceId": -3}, compare=exact)
    await mcp.call_tool("get-resource-reference", {"resourceType": "Image"}, compare=exact)
    await mcp.call_tool("get-resource-reference", {"resourceId": "1"}, compare=exact)


async def test_prompts(mcp: McpClient) -> None:
    simple: Any = await mcp.request(
        "prompts/get", {"name": "simple-prompt", "arguments": {"a": "b"}}
    )
    assert isinstance(simple, dict)
    weather: Any = await mcp.request(
        "prompts/get", {"name": "args-prompt", "arguments": {"city": "Denver", "state": "CO"}}
    )
    assert isinstance(weather, dict)
    assert weather["messages"][0]["content"]["text"] == "What's weather in Denver, CO?"
    await mcp.request("prompts/get", {"name": "args-prompt", "arguments": {"city": "Lyon"}})
    team: Any = await mcp.request(
        "prompts/get",
        {"name": "completable-prompt", "arguments": {"department": "Support", "name": "Kim"}},
    )
    assert isinstance(team, dict)
    assert "Kim" in team["messages"][0]["content"]["text"]


async def test_prompt_errors(mcp: McpClient) -> None:
    await mcp.request("prompts/get", {"name": "args-prompt", "arguments": {"state": "CO"}})
    await mcp.request("prompts/get", {"name": "completable-prompt", "arguments": {}})
    await mcp.request("prompts/get", {"name": "args-prompt", "arguments": {"city": True}})
    await mcp.request("prompts/get", {"name": "args-prompt", "arguments": ["Denver"]})
    await mcp.request("prompts/get", {"name": "weather-prompt"})
    await mcp.request("prompts/get", {"name": 7})
    await mcp.request(
        "prompts/get",
        {"name": "resource-prompt", "arguments": {"resourceType": "Audio", "resourceId": "1"}},
    )
    await mcp.request(
        "prompts/get",
        {"name": "resource-prompt", "arguments": {"resourceType": "Text", "resourceId": "-2"}},
    )
    await mcp.request(
        "prompts/get", {"name": "resource-prompt", "arguments": {"resourceType": "Blob"}}
    )


async def test_resource_prompt_embeds_a_dynamic_resource(mcp: McpClient) -> None:
    live = ignoring("$.messages[1].content.resource.text", "$.messages[1].content.resource.blob")
    prompt: Any = await mcp.request(
        "prompts/get",
        {"name": "resource-prompt", "arguments": {"resourceType": "Text", "resourceId": "009"}},
        compare=live,
    )
    assert isinstance(prompt, dict)
    assert prompt["messages"][0]["content"]["text"].startswith(
        "This prompt includes the Text resource with id: 9."
    )
    assert_recent_local_time(prompt["messages"][1]["content"]["resource"]["text"])
    await mcp.request(
        "prompts/get",
        {"name": "resource-prompt", "arguments": {"resourceType": "Blob", "resourceId": "31"}},
        compare=live,
    )


def prompt_ref(name: str) -> JsonObject:
    return {"type": "ref/prompt", "name": name}


async def complete(mcp: McpClient, ref: Any, name: str, value: str, **context: str) -> Any:
    params: dict[str, Any] = {"ref": ref, "argument": {"name": name, "value": value}}
    if context:
        params["context"] = {"arguments": context}
    return await mcp.request("completion/complete", params)


async def test_completions_narrow_by_prefix_and_context(mcp: McpClient) -> None:
    team = prompt_ref("completable-prompt")
    assert await complete(mcp, team, "department", "Su") == {
        "completion": {"values": ["Support"], "total": 1, "hasMore": False}
    }
    await complete(mcp, team, "department", "M")
    await complete(mcp, team, "department", "Engineering")
    names: Any = await complete(mcp, team, "name", "", department="Marketing")
    assert isinstance(names, dict)
    assert names["completion"]["values"] == ["Grace", "Henry", "Iris"]
    await complete(mcp, team, "name", "L", department="Support")
    await complete(mcp, team, "name", "C", department="Engineering")
    await complete(mcp, team, "name", "", department="HR")
    resource = prompt_ref("resource-prompt")
    await complete(mcp, resource, "resourceType", "B")
    await complete(mcp, resource, "resourceId", "17")
    await complete(mcp, resource, "resourceId", "-1")
    await complete(mcp, resource, "resourceId", "3.5")
    await complete(mcp, prompt_ref("args-prompt"), "state", "W")


async def test_template_completions_and_errors(mcp: McpClient) -> None:
    text = {"type": "ref/resource", "uri": "demo://resource/dynamic/text/{resourceId}"}
    await complete(mcp, text, "resourceId", "21")
    await complete(mcp, text, "resourceId", "0")
    await complete(mcp, text, "resourceId", "")
    await complete(mcp, {"type": "ref/resource", "uri": f"{DOCS}/structure.md"}, "x", "y")
    await complete(mcp, {"type": "ref/resource", "uri": "demo://nothing/{id}"}, "id", "1")
    await complete(mcp, prompt_ref("missing-prompt"), "a", "b")
    await mcp.request(
        "completion/complete", {"ref": "completable-prompt", "argument": {"name": "a", "value": ""}}
    )
    await mcp.request(
        "completion/complete",
        {"ref": {"type": "ref/prompt"}, "argument": {"name": "a", "value": ""}},
    )
    await mcp.request("completion/complete", {"ref": prompt_ref("args-prompt")})
    await mcp.request(
        "completion/complete", {"ref": prompt_ref("args-prompt"), "argument": {"name": "city"}}
    )


async def test_logging_levels(mcp: McpClient) -> None:
    for level in ("warning", "debug", "emergency"):
        assert await mcp.request("logging/setLevel", {"level": level}) == {}
    await mcp.request("logging/setLevel", {"level": "trace"})
    await mcp.request("logging/setLevel", {"level": 3})
