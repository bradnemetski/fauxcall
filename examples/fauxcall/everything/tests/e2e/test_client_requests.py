"""Tools that ask the client: sampling, elicitation (form and URL), roots, and their async forms.

Each opens the session as a client with the needed capabilities and scripted replies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fauxcall.testing.client import CompareOptions
from scenario_support import ALL_CAPABILITIES, texts

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

ASSISTANT: dict[str, Any] = {
    "role": "assistant",
    "model": "scenario-model",
    "content": {"type": "text", "text": "A short answer."},
    "stopReason": "endTurn",
}


async def test_sampling_listing_and_answer(mcp: McpClient) -> None:
    await mcp.open(capabilities={"sampling": {}}, replies={"sampling/createMessage": ASSISTANT})
    listed: Any = await mcp.list_tools()
    names = [tool["name"] for tool in listed["tools"]]
    assert "trigger-sampling-request" in names
    assert "trigger-sampling-request-async" not in names
    result: Any = await mcp.call_tool(
        "trigger-sampling-request", {"prompt": "Name three rivers", "maxTokens": 64}
    )
    assert texts(result)[0].startswith("LLM sampling result: \n{\n")


async def test_sampling_answers_in_other_shapes(mcp: McpClient) -> None:
    reply: dict[str, Any] = {
        "content": {"type": "text", "text": "Unordered keys"},
        "role": "assistant",
        "model": "m-2",
        "stopReason": "stopSequence",
        "_meta": {"trace": "abc"},
    }
    await mcp.open(capabilities={"sampling": {}}, replies={"sampling/createMessage": reply})
    await mcp.call_tool("trigger-sampling-request", {"prompt": "order?"})
    mcp.answer_with({"sampling/createMessage": {"error": {"code": -32603, "message": "LLM down"}}})
    refused: Any = await mcp.call_tool("trigger-sampling-request", {"prompt": "fail"})
    assert refused["isError"] is True
    mcp.answer_with({"sampling/createMessage": {"role": "user", "content": {}}})
    await mcp.call_tool("trigger-sampling-request", {"prompt": "no model"})
    await mcp.call_tool("trigger-sampling-request", {"prompt": 7})


async def test_sampling_tool_hidden_without_capability(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("trigger-sampling-request", {"prompt": "hello"})
    assert result["isError"] is True
    await mcp.call_tool("get-roots-list", {})


async def test_form_elicitation(mcp: McpClient) -> None:
    accepted: dict[str, Any] = {
        "action": "accept",
        "content": {
            "name": "Katherine Johnson",
            "check": True,
            "email": "kj@example.org",
            "birthdate": "1918-08-26",
            "integer": 100,
            "number": 999.5,
            "untitledSingleSelectEnum": "Ross",
        },
    }
    await mcp.open(capabilities={"elicitation": {}}, replies={"elicitation/create": accepted})
    result: Any = await mcp.call_tool("trigger-elicitation-request", {})
    assert texts(result)[0] == "✅ User provided the requested information!"
    mcp.answer_with(
        {"elicitation/create": {"action": "accept", "content": {"name": "", "check": False}}}
    )
    await mcp.call_tool("trigger-elicitation-request", {})
    mcp.answer_with({"elicitation/create": {"action": "cancel", "content": {"name": "x"}}})
    await mcp.call_tool("trigger-elicitation-request", {})
    url_hidden: Any = await mcp.call_tool("trigger-url-elicitation", {"url": "https://x.org"})
    assert url_hidden["isError"] is True


async def test_url_elicitation_paths(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {"form": {}, "url": {}}},
        replies={"elicitation/create": {"action": "decline"}},
    )
    declined: Any = await mcp.call_tool(
        "trigger-url-elicitation",
        {"url": "https://example.org/connect", "message": "Link your account"},
    )
    assert texts(declined)[0].startswith("❌ User declined to open the URL (Elicitation ID: ")
    exact = CompareOptions()  # the caller's id: the same on both sides
    await mcp.call_tool(
        "trigger-url-elicitation",
        {"url": "https://example.org/a", "elicitationId": "scenario-id-7"},
        compare=exact,
    )
    first: Any = await mcp.call_tool(
        "trigger-url-elicitation",
        {"url": "https://example.org/b", "errorPath": True, "elicitationId": "scenario-id-8"},
    )
    assert first["code"] == -32042  # the error path: URL elicitation required
    assert first["data"]["elicitations"][0]["url"] == "https://modelcontextprotocol.io"
    mcp.answer_with({"elicitation/create": {"action": "accept"}})
    retried: Any = await mcp.call_tool(
        "trigger-url-elicitation",
        {"url": "https://example.org/b", "errorPath": True, "elicitationId": "scenario-id-8"},
        compare=exact,
    )
    assert texts(retried)[0] == (
        "✅ User completed the URL elicitation flow.\n"
        "Elicitation ID: scenario-id-8\nURL: https://example.org/b"
    )
    await mcp.call_tool("trigger-url-elicitation", {"url": "relative/path"})


async def test_async_sampling_through_a_client_task(mcp: McpClient) -> None:
    task: dict[str, Any] = {
        "task": {"taskId": "scenario-task-1", "status": "working", "pollInterval": 500}
    }
    replies: dict[str, Any] = {
        "sampling/createMessage": task,
        "tasks/get": {"taskId": "scenario-task-1", "status": "completed"},
        "tasks/result": {
            "role": "assistant",
            "model": "async-model",
            "content": {"type": "text", "text": "done"},
        },
    }
    await mcp.open(capabilities=ALL_CAPABILITIES, replies=replies)
    result: Any = await mcp.call_tool(
        "trigger-sampling-request-async", {"prompt": "Long task", "maxTokens": 300}
    )
    assert texts(result)[0].startswith("[COMPLETED] Async sampling completed!")
    mcp.answer_with({"sampling/createMessage": {**ASSISTANT, "extra": "dropped"}})
    synced: Any = await mcp.call_tool("trigger-sampling-request-async", {"prompt": "Quick"})
    assert "extra" not in texts(synced)[0]


async def test_async_elicitation_through_a_client_task(mcp: McpClient) -> None:
    replies: dict[str, Any] = {
        "elicitation/create": {"task": {"taskId": "scenario-task-2", "status": "working"}},
        "tasks/get": {"status": "cancelled", "statusMessage": "User closed the dialog"},
    }
    await mcp.open(capabilities=ALL_CAPABILITIES, replies=replies)
    cancelled: Any = await mcp.call_tool("trigger-elicitation-request-async", {})
    assert texts(cancelled)[0].startswith("[CANCELLED] User closed the dialog")
    mcp.answer_with(
        {
            "elicitation/create": {
                "action": "accept",
                "content": {"name": "Ana", "favoriteColor": "Purple"},
            }
        }
    )
    await mcp.call_tool("trigger-elicitation-request-async", {})


async def test_roots_without_a_get_stream(mcp: McpClient) -> None:
    """The server asks for roots on the GET stream; this client has none open."""
    await mcp.open(capabilities={"roots": {}}, replies={"roots/list": {"roots": []}})
    result: Any = await mcp.call_tool("get-roots-list", {})
    assert texts(result)[0].startswith("The client supports roots but no roots are currently")
