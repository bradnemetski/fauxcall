"""Tools that ask the client mid-call: sampling, elicitation (form and URL), roots, async tasks.

The scenario's client declares the capabilities and answers the server's requests the same way
on both sides; ``notifications=True`` compares the requests the server sent during the call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scenario_support import ELICITATION_ID, text_of

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject

ANSWER: JsonObject = {
    "role": "assistant",
    "content": {"type": "text", "text": "Forty-two."},
    "model": "parity-model",
    "stopReason": "endTurn",
}


async def test_sampling(mcp: McpClient) -> None:
    await mcp.open(capabilities={"sampling": {}}, replies={"sampling/createMessage": ANSWER})
    result: Any = await mcp.call_tool(
        "trigger-sampling-request",
        {"prompt": "What is six times seven?", "maxTokens": 20},
        notifications=True,
    )
    assert text_of(result).startswith('LLM sampling result: \n{\n  "model": "parity-model"')
    mcp.answer_with({"sampling/createMessage": {"error": {"code": -32600, "message": "declined"}}})
    refused: Any = await mcp.call_tool("trigger-sampling-request", {"prompt": "Again?"})
    assert text_of(refused) == "MCP error -32600: declined"
    mcp.answer_with({"sampling/createMessage": {"role": "robot", "model": 5}})
    await mcp.call_tool("trigger-sampling-request", {"prompt": "Broken answer"})
    await mcp.call_tool("trigger-sampling-request", {"prompt": ["list"]})


async def test_form_elicitation(mcp: McpClient) -> None:
    content: JsonObject = {
        "name": "Parity Tester",
        "check": False,
        "email": "tester@example.org",
        "integer": 7,
        "number": 2.5,
        "color": "teal",
        "petType": "dog",
        "untitledMultipleSelectEnum": ["Drums"],
    }
    await mcp.open(
        capabilities={"elicitation": {"form": {}}},
        replies={"elicitation/create": {"action": "accept", "content": content}},
    )
    accepted: Any = await mcp.call_tool("trigger-elicitation-request", {}, notifications=True)
    assert isinstance(accepted, dict)
    assert "- Favorite Color: teal" in text_of(accepted, 1)
    mcp.answer_with({"elicitation/create": {"action": "decline"}})
    declined: Any = await mcp.call_tool("trigger-elicitation-request", {})
    assert text_of(declined).startswith("❌")
    mcp.answer_with({"elicitation/create": {"action": "cancel", "content": None}})
    await mcp.call_tool("trigger-elicitation-request", {})


async def test_url_elicitation(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {"url": {}}},
        replies={"elicitation/create": {"action": "accept"}},
    )
    accepted: Any = await mcp.call_tool(
        "trigger-url-elicitation",
        {"url": "https://example.org/consent", "elicitationId": "parity-1"},
        notifications=True,
    )
    assert "Elicitation ID: parity-1" in text_of(accepted)
    arguments: JsonObject = {
        "url": "https://example.org/sso",
        "errorPath": True,
        "elicitationId": "parity-2",
    }
    required: Any = await mcp.request(
        "tools/call",
        {"name": "trigger-url-elicitation", "arguments": arguments},
        compare=ELICITATION_ID,
    )
    assert isinstance(required, dict)
    assert required["code"] == -32042
    retried: Any = await mcp.call_tool("trigger-url-elicitation", arguments)
    assert "Elicitation ID: parity-2" in text_of(retried)
    mcp.answer_with({"elicitation/create": {"action": "decline"}})
    await mcp.call_tool(
        "trigger-url-elicitation",
        {"url": "https://example.org/pay", "message": "Pay now", "elicitationId": "parity-3"},
    )
    await mcp.call_tool("trigger-url-elicitation", {"url": "example dot org"})


async def test_roots_are_asked_for_after_initialization(mcp: McpClient) -> None:
    roots: JsonObject = {
        "roots": [{"uri": "file:///workspace/app", "name": "App"}, {"uri": "file:///lib"}]
    }
    await mcp.open(capabilities={"roots": {"listChanged": True}}, replies={"roots/list": roots})
    # the server asks 350 ms after initialization on the GET stream: it must be open by then
    # (it opens within milliseconds here); later, the request would be dropped on both sides
    await mcp.listen()
    await mcp.heard("roots/list", count=1, within=5)
    logged: Any = await mcp.heard("notifications/message", count=1, within=5, values=True)
    assert "Roots updated: 2 root(s) received from client" in str(logged)
    listed: Any = await mcp.call_tool("get-roots-list", {})
    assert text_of(listed).startswith("Current MCP Roots (2 total):\n\n1. App\n")


async def test_roots_refused_then_given(mcp: McpClient) -> None:
    refusal: JsonObject = {"error": {"code": -32601, "message": "Method not found"}}
    await mcp.open(capabilities={"roots": {}}, replies={"roots/list": refusal})
    await mcp.listen()
    await mcp.heard("roots/list", count=1, within=5)
    none_yet: Any = await mcp.call_tool("get-roots-list", {})
    assert text_of(none_yet).startswith("The client supports roots but no roots")
    mcp.answer_with({"roots/list": {"roots": [{"uri": "file:///srv/data", "name": "Data"}]}})
    given: Any = await mcp.call_tool("get-roots-list", {})
    assert "1. Data\n   URI: file:///srv/data" in text_of(given)
    await mcp.heard("notifications/message", count=1, within=5, values=True)


async def test_async_sampling_as_a_client_task(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"sampling": {}, "tasks": {"requests": {"sampling": {"createMessage": {}}}}},
        replies={
            "sampling/createMessage": {"task": {"taskId": "client-77", "status": "working"}},
            "tasks/get": {"taskId": "client-77", "status": "completed", "statusMessage": "done"},
            "tasks/result": {
                "model": "m1",
                "role": "assistant",
                "content": {"type": "text", "text": "Hi"},
            },
        },
    )
    polled: Any = await mcp.call_tool(
        "trigger-sampling-request-async", {"prompt": "Say hi", "maxTokens": 8}, notifications=True
    )
    assert text_of(polled).startswith("[COMPLETED] Async sampling completed!")
    mcp.answer_with({"sampling/createMessage": ANSWER})
    direct: Any = await mcp.call_tool("trigger-sampling-request-async", {"prompt": "Quick"})
    assert text_of(direct).startswith("[SYNC] Client executed synchronously:")


async def test_async_elicitation_as_a_client_task(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {}, "tasks": {"requests": {"elicitation": {"create": {}}}}},
        replies={
            "elicitation/create": {"task": {"taskId": "form-9", "status": "input_required"}},
            "tasks/get": {"taskId": "form-9", "status": "cancelled", "statusMessage": "closed"},
        },
    )
    cancelled: Any = await mcp.call_tool(
        "trigger-elicitation-request-async", {}, notifications=True
    )
    assert text_of(cancelled).startswith("[CANCELLED] closed")
    mcp.answer_with(
        {
            "elicitation/create": {"task": {"taskId": "form-10", "status": "working"}},
            "tasks/get": {"taskId": "form-10", "status": "completed"},
            "tasks/result": {"action": "accept", "content": {"name": "Lin", "agreeToTerms": True}},
        }
    )
    accepted: Any = await mcp.call_tool("trigger-elicitation-request-async", {})
    assert "- Agreed to terms: true" in text_of(accepted, 1)


async def test_client_answers_are_validated(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"sampling": {}, "tasks": {"requests": {"sampling": {"createMessage": {}}}}},
        replies={"sampling/createMessage": {}},
    )
    empty: Any = await mcp.call_tool("trigger-sampling-request-async", {"prompt": "empty"})
    assert '"code": "invalid_union"' in text_of(empty)


async def test_form_values_must_be_simple(mcp: McpClient) -> None:
    nested: JsonObject = {"action": "accept", "content": {"name": {"first": "Ada"}}}
    await mcp.open(capabilities={"elicitation": {}}, replies={"elicitation/create": nested})
    refused: Any = await mcp.call_tool("trigger-elicitation-request", {})
    assert '"content",\n      "name"' in text_of(refused)


async def test_urls_are_trimmed(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {"url": {}}},
        replies={"elicitation/create": {"action": "accept"}},
    )
    padded: Any = await mcp.call_tool(
        "trigger-url-elicitation",
        {"url": "  https://example.org/padded\n", "elicitationId": "trim-e2e"},
        notifications=True,
    )
    assert "URL: https://example.org/padded" in text_of(padded)
    ftp: Any = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "p.gz", "data": " ftp://x.example/f "}
    )
    assert text_of(ftp).startswith("Error processing file ftp://x.example/f: ")
