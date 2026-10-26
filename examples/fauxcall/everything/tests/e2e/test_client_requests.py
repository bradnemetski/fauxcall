"""Tools that ask the client mid-call: sampling, elicitation (form, URL), roots, task variants."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from everything_support import text_reply
from fauxcall.model.fields import RandomPolicy
from fauxcall.testing import CompareOptions

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject

SAMPLING: JsonObject = {"sampling": {}}
FORM: JsonObject = {"elicitation": {}}
URL: JsonObject = {"elicitation": {"form": {}, "url": {}}}
TASKS: JsonObject = {
    "sampling": {},
    "elicitation": {},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


def _texts(result: object) -> list[str]:
    assert isinstance(result, dict)
    return [str(block.get("text", "")) for block in result["content"] if isinstance(block, dict)]


async def test_sampling_round_trip(mcp: McpClient) -> None:
    await mcp.open(
        capabilities=SAMPLING,
        replies={"sampling/createMessage": text_reply("Paris.", stop="endTurn")},
    )
    result: Any = await mcp.call_tool(
        "trigger-sampling-request",
        {"prompt": "Capital of France?", "maxTokens": 12},
        notifications=True,
    )
    assert _texts(result)[0].startswith("LLM sampling result: \n{")


async def test_sampling_answers_parsed_like_the_sdk(mcp: McpClient) -> None:
    await mcp.open(capabilities=SAMPLING)
    mcp.answer_with(
        {
            "sampling/createMessage": {
                "content": {"mimeType": "audio/wav", "data": "UklGRg==", "type": "audio", "x": 1},
                "role": "user",
                "model": "audio-model",
                "custom": {"kept": True},
            }
        }
    )
    await mcp.call_tool("trigger-sampling-request", {"prompt": "Hum a tune"})
    mcp.answer_with({"sampling/createMessage": {"role": "assistant", "model": 7, "content": {}}})
    await mcp.call_tool("trigger-sampling-request", {"prompt": "Bad answer"})


async def test_sampling_refused_by_the_client(mcp: McpClient) -> None:
    await mcp.open(
        capabilities=SAMPLING,
        replies={
            "sampling/createMessage": {"error": {"code": -32000, "message": "No model available"}}
        },
    )
    result: Any = await mcp.call_tool("trigger-sampling-request", {"prompt": "Anyone there?"})
    assert isinstance(result, dict)
    assert result["isError"] is True
    await mcp.call_tool("trigger-sampling-request", {"maxTokens": 5})


async def test_sampling_tool_unknown_without_the_capability(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool("trigger-sampling-request", {"prompt": "hello?"})
    assert isinstance(result, dict)
    assert result["isError"] is True


async def test_form_elicitation(mcp: McpClient) -> None:
    form: JsonObject = {
        "name": "Rosalind Franklin",
        "check": True,
        "email": "rosalind@example.org",
        "integer": 51,
        "number": 0.25,
        "birthdate": "1920-07-25",
        "untitledMultipleSelectEnum": ["Drums"],
    }
    await mcp.open(
        capabilities=FORM, replies={"elicitation/create": {"action": "accept", "content": form}}
    )
    result: Any = await mcp.call_tool("trigger-elicitation-request", {}, notifications=True)
    assert _texts(result)[0] == "✅ User provided the requested information!"
    mcp.answer_with({"elicitation/create": {"action": "cancel", "content": None}})
    await mcp.call_tool("trigger-elicitation-request", {})
    mcp.answer_with(
        {"elicitation/create": {"action": "accept", "content": {"name": "", "check": False}}}
    )
    await mcp.call_tool("trigger-elicitation-request", {})


async def test_form_elicitation_declined_or_refused(mcp: McpClient) -> None:
    await mcp.open(capabilities=FORM, replies={"elicitation/create": {"action": "decline"}})
    await mcp.call_tool("trigger-elicitation-request", {})
    mcp.answer_with(
        {"elicitation/create": {"error": {"code": -1, "message": "User closed the window"}}}
    )
    result: Any = await mcp.call_tool("trigger-elicitation-request", {})
    assert isinstance(result, dict)
    assert result["isError"] is True
    mcp.answer_with(
        {"elicitation/create": {"action": "accept", "content": {"name": {"first": "A"}}}}
    )
    await mcp.call_tool("trigger-elicitation-request", {})


async def test_url_elicitation_request_path(mcp: McpClient) -> None:
    await mcp.open(capabilities=URL, replies={"elicitation/create": {"action": "accept"}})
    result: Any = await mcp.call_tool(
        "trigger-url-elicitation",
        {
            "url": "https://example.com/oauth/start",
            "elicitationId": "parity-1",
            "message": "Sign in",
        },
        notifications=True,
    )
    assert "Elicitation ID: parity-1" in _texts(result)[0]
    mcp.answer_with({"elicitation/create": {"action": "cancel"}})
    await mcp.call_tool(
        "trigger-url-elicitation", {"url": "https://example.com/pay", "elicitationId": "parity-2"}
    )


async def test_url_elicitation_error_path_then_retry(mcp: McpClient) -> None:
    await mcp.open(capabilities=URL, replies={"elicitation/create": {"action": "decline"}})
    arguments: JsonObject = {
        "url": "https://example.com/consent",
        "errorPath": True,
        "elicitationId": "parity-3",
    }
    random_prerequisite = CompareOptions(
        policies=[
            RandomPolicy(
                path="$.data.elicitations[*].elicitationId", scope="body", generator="uuid4"
            )
        ]
    )
    first: Any = await mcp.call_tool(
        "trigger-url-elicitation", arguments, compare=random_prerequisite
    )
    assert isinstance(first, dict)
    assert first["code"] == -32042
    retry: Any = await mcp.call_tool("trigger-url-elicitation", arguments)
    assert "parity-3" in _texts(retry)[0]
    third: Any = await mcp.call_tool(
        "trigger-url-elicitation", arguments, compare=random_prerequisite
    )
    assert isinstance(third, dict)
    assert third["code"] == -32042


async def test_url_elicitation_refuses_bad_urls(mcp: McpClient) -> None:
    await mcp.open(capabilities=URL)
    await mcp.call_tool("trigger-url-elicitation", {"url": "example.com/no-scheme"})
    await mcp.call_tool("trigger-url-elicitation", {"url": "https://example.com", "message": 5})


async def test_roots_are_asked_once_after_initialization(mcp: McpClient) -> None:
    roots: JsonObject = {
        "roots": [
            {"uri": "file:///srv/repos/app", "name": "app"},
            {"uri": "file:///srv/repos/lib", "name": ""},
            {"uri": "file:///tmp"},
        ]
    }
    await mcp.open(capabilities={"roots": {"listChanged": True}}, replies={"roots/list": roots})
    await mcp.listen()
    asked: Any = await mcp.heard("roots/list", count=1, within=5)
    assert asked[0]["id"] == 0
    logged: Any = await mcp.heard("notifications/message", count=1, within=5)
    assert logged[0]["params"] == {
        "level": "info",
        "logger": "everything-server",
        "data": "Roots updated: 3 root(s) received from client",
    }
    result: Any = await mcp.call_tool("get-roots-list", {})
    assert _texts(result)[0].startswith("Current MCP Roots (3 total):")
    mcp.answer_with({"roots/list": {"roots": []}})
    again: Any = await mcp.call_tool("get-roots-list", {})
    assert _texts(again) == _texts(result)


async def test_empty_roots(mcp: McpClient) -> None:
    await mcp.open(capabilities={"roots": {}}, replies={"roots/list": {"roots": []}})
    await mcp.listen()
    await mcp.heard("roots/list", count=1, within=5)
    result: Any = await mcp.call_tool("get-roots-list", {})
    assert _texts(result)[0].startswith("The client supports roots but no roots")


async def test_async_sampling_task_completes(mcp: McpClient) -> None:
    await mcp.open(
        capabilities=TASKS,
        replies={
            "sampling/createMessage": {"task": {"taskId": "parity-task-1", "status": "working"}},
            "tasks/get": {"taskId": "parity-task-1", "status": "completed", "statusMessage": "ok"},
            "tasks/result": text_reply("Async answer"),
        },
    )
    result: Any = await mcp.call_tool(
        "trigger-sampling-request-async",
        {"prompt": "Do it later", "maxTokens": 30},
        notifications=True,
    )
    assert _texts(result)[0].startswith("[COMPLETED] Async sampling completed!")


async def test_async_sampling_sync_answer_and_failure(mcp: McpClient) -> None:
    await mcp.open(capabilities=TASKS, replies={"sampling/createMessage": text_reply("Right away")})
    await mcp.call_tool("trigger-sampling-request-async", {"prompt": "Now please"})
    mcp.answer_with(
        {
            "sampling/createMessage": {"task": {"taskId": "parity-task-2", "status": "working"}},
            "tasks/get": {"status": "failed", "statusMessage": "GPU on fire"},
        }
    )
    result: Any = await mcp.call_tool("trigger-sampling-request-async", {"prompt": "Fail"})
    assert _texts(result)[0].startswith("[FAILED] GPU on fire")


async def test_async_elicitation(mcp: McpClient) -> None:
    await mcp.open(
        capabilities=TASKS,
        replies={
            "elicitation/create": {"task": {"taskId": "parity-el-1", "status": "input_required"}},
            "tasks/get": {"status": "completed"},
            "tasks/result": {
                "action": "accept",
                "content": {"name": "Ada", "favoriteColor": "Blue"},
            },
        },
    )
    result: Any = await mcp.call_tool("trigger-elicitation-request-async", {}, notifications=True)
    assert _texts(result)[0] == "[COMPLETED] User provided the requested information!"
    mcp.answer_with({"elicitation/create": {"action": "decline"}})
    await mcp.call_tool("trigger-elicitation-request-async", {})
    mcp.answer_with(
        {
            "elicitation/create": {"task": {"taskId": "parity-el-2", "status": "working"}},
            "tasks/get": {"status": "cancelled"},
        }
    )
    await mcp.call_tool("trigger-elicitation-request-async", {})
