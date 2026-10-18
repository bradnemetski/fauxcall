"""Tools that ask the client mid-call: sampling, elicitation (form and URL), client-side tasks.

The session declares the capability and answers the server's requests with fixed replies; the
tools' answers must report those replies the way the real server does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
from policies import PREREQUISITE

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject

SAMPLING: Final[JsonObject] = {"sampling": {}}
ELICITATION: Final[JsonObject] = {"elicitation": {"form": {}, "url": {}}}
REPLY: Final[JsonObject] = {
    "role": "assistant",
    "content": {"type": "text", "text": "Ola!"},
    "model": "e2e-model",
    "stopReason": "stopSequence",
}


def text(result: Any, index: int = 0) -> str:
    return str(result["content"][index]["text"])


async def test_sampling_reports_the_clients_answer(mcp: McpClient) -> None:
    await mcp.open(capabilities=SAMPLING, replies={"sampling/createMessage": REPLY})
    result: Any = await mcp.call_tool(
        "trigger-sampling-request", {"prompt": "Greet in Portuguese", "maxTokens": 12}
    )
    assert text(result) == (
        'LLM sampling result: \n{\n  "model": "e2e-model",\n  "stopReason": "stopSequence",\n'
        '  "role": "assistant",\n  "content": {\n    "type": "text",\n    "text": "Ola!"\n  }\n}'
    )


@pytest.mark.parametrize(
    "reply",
    [
        {"error": {"code": -32000, "message": "user said no"}},
        {"role": "assistant", "content": {"type": "text", "text": "no model"}},
        {"role": "narrator", "model": "m", "content": {"type": "text", "text": "x"}},
    ],
)
async def test_sampling_refusals(mcp: McpClient, reply: JsonObject) -> None:
    await mcp.open(capabilities=SAMPLING, replies={"sampling/createMessage": reply})
    result: Any = await mcp.call_tool("trigger-sampling-request", {"prompt": "x"})
    assert result["isError"] is True


async def test_elicitation_outcomes(mcp: McpClient) -> None:
    await mcp.open(
        capabilities=ELICITATION,
        replies={
            "elicitation/create": {
                "action": "accept",
                "content": {
                    "name": "Grace Hopper",
                    "email": "grace@example.com",
                    "integer": 7,
                    "check": True,
                },
            }
        },
    )
    accepted: Any = await mcp.call_tool("trigger-elicitation-request", {})
    assert text(accepted, 1) == (
        "User inputs:\n- Name: Grace Hopper\n- Agreed to terms: true\n- Email: grace@example.com\n"
        "- Favorite Integer: 7"
    )
    mcp.answer_with({"elicitation/create": {"action": "decline"}})
    declined: Any = await mcp.call_tool("trigger-elicitation-request", {})
    assert text(declined) == "❌ User declined to provide the requested information."
    mcp.answer_with(
        {"elicitation/create": {"error": {"code": -32603, "message": "dialog crashed"}}}
    )
    failed: Any = await mcp.call_tool("trigger-elicitation-request", {})
    assert failed == {
        "content": [{"type": "text", "text": "MCP error -32603: dialog crashed"}],
        "isError": True,
    }


async def test_url_elicitation_request_path(mcp: McpClient) -> None:
    await mcp.open(capabilities=ELICITATION, replies={"elicitation/create": {"action": "accept"}})
    accepted: Any = await mcp.call_tool(
        "trigger-url-elicitation", {"url": "https://example.org/consent", "elicitationId": "e2e-1"}
    )
    assert text(accepted) == (
        "✅ User completed the URL elicitation flow.\nElicitation ID: e2e-1\nURL: https://example.org/consent"
    )
    mcp.answer_with({"elicitation/create": {"action": "cancel"}})
    cancelled: Any = await mcp.call_tool(
        "trigger-url-elicitation",
        {"url": "https://example.org/x", "message": "Open it", "elicitationId": "e2e-2"},
    )
    assert text(cancelled) == "⚠️ User cancelled the URL elicitation (Elicitation ID: e2e-2)."


async def test_url_elicitation_error_path_lets_the_retry_through(mcp: McpClient) -> None:
    await mcp.open(capabilities=ELICITATION, replies={"elicitation/create": {"action": "decline"}})
    arguments: JsonObject = {"url": "https://example.org/login", "errorPath": True}
    first: Any = await mcp.call_tool("trigger-url-elicitation", arguments, compare=PREREQUISITE)
    assert first["code"] == -32042
    assert first["data"]["elicitations"][0]["url"] == "https://modelcontextprotocol.io"
    retried: Any = await mcp.call_tool(
        "trigger-url-elicitation", {**arguments, "elicitationId": "e2e-3"}, compare=PREREQUISITE
    )
    assert retried["code"] == -32042  # another call: a different key
    again: Any = await mcp.call_tool(
        "trigger-url-elicitation", {**arguments, "elicitationId": "e2e-3"}
    )
    assert text(again) == "❌ User declined to open the URL (Elicitation ID: e2e-3)."


async def test_url_elicitation_refusals(mcp: McpClient) -> None:
    await mcp.open(capabilities=ELICITATION)
    relative: Any = await mcp.call_tool("trigger-url-elicitation", {"url": "/relative/path"})
    flag: Any = await mcp.call_tool(
        "trigger-url-elicitation", {"url": "https://x.org", "errorPath": "yes"}
    )
    assert relative["isError"] is True
    assert "Invalid URL at url" in text(relative)
    assert flag["isError"] is True


CLIENT_TASKS: Final[JsonObject] = {
    "sampling": {},
    "elicitation": {},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


async def test_async_sampling_sync_and_task(mcp: McpClient) -> None:
    await mcp.open(capabilities=CLIENT_TASKS, replies={"sampling/createMessage": REPLY})
    direct: Any = await mcp.call_tool("trigger-sampling-request-async", {"prompt": "hi"})
    assert text(direct).startswith(
        '[SYNC] Client executed synchronously:\n{\n  "role": "assistant"'
    )
    mcp.answer_with(
        {
            "sampling/createMessage": {"task": {"taskId": "client-7", "status": "working"}},
            "tasks/get": {"taskId": "client-7", "status": "completed", "statusMessage": "ready"},
            "tasks/result": REPLY,
        }
    )
    tasked: Any = await mcp.call_tool(
        "trigger-sampling-request-async", {"prompt": "hi", "maxTokens": 5}
    )
    assert text(tasked).startswith(
        "[COMPLETED] Async sampling completed!\n\n**Progress:**\nTask created: client-7\n"
        "Poll 1: completed - ready\n\n**Result:**\n"
    )


async def test_async_elicitation_task_outcomes(mcp: McpClient) -> None:
    await mcp.open(
        capabilities=CLIENT_TASKS,
        replies={
            "elicitation/create": {"task": {"taskId": "client-8", "status": "input_required"}},
            "tasks/get": {"taskId": "client-8", "status": "completed"},
            "tasks/result": {"action": "accept", "content": {"name": "Ada", "agreeToTerms": True}},
        },
    )
    accepted: Any = await mcp.call_tool("trigger-elicitation-request-async", {})
    assert text(accepted, 1) == "User inputs:\n- Name: Ada\n- Agreed to terms: true"
    mcp.answer_with(
        {
            "elicitation/create": {"task": {"taskId": "client-9", "status": "working"}},
            "tasks/get": {
                "taskId": "client-9",
                "status": "failed",
                "statusMessage": "user went away",
            },
        }
    )
    failed: Any = await mcp.call_tool("trigger-elicitation-request-async", {})
    assert text(failed) == (
        "[FAILED] user went away\n\nProgress:\nTask created: client-9\n"
        "Poll 1: failed - user went away"
    )
