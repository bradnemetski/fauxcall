"""Tools that ask the client (sampling, elicitation, roots, client tasks), and who sees them."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from scenario_support import (
    PLAIN_TOOLS,
    TEXTS,
    UUID,
    dig,
    task_times,
    text,
    texts,
    tool_call,
    varying,
)

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject

SAMPLED: JsonObject = {
    "role": "assistant",
    "content": {"type": "text", "text": "Rivers carve the valley floor."},
    "model": "scenario-model",
    "stopReason": "endTurn",
}


async def test_sampling(mcp: McpClient) -> None:
    await mcp.open(capabilities={"sampling": {}}, replies={"sampling/createMessage": SAMPLED})
    result = await mcp.call_tool(
        "trigger-sampling-request", {"prompt": "Describe a river", "maxTokens": 64}
    )
    assert text(result) == (
        "LLM sampling result: \n{\n"
        '  "model": "scenario-model",\n'
        '  "stopReason": "endTurn",\n'
        '  "role": "assistant",\n'
        '  "content": {\n'
        '    "type": "text",\n'
        '    "text": "Rivers carve the valley floor."\n'
        "  }\n}"
    )


async def test_sampling_refused_by_the_client(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"sampling": {}},
        replies={
            "sampling/createMessage": {"error": {"code": -32600, "message": "No model available"}}
        },
    )
    result = await mcp.call_tool("trigger-sampling-request", {"prompt": "Anything"})
    assert dig(result, "isError") is True
    assert text(result) == "MCP error -32600: No model available"


@pytest.mark.parametrize(
    "reply",
    [
        {"role": "robot", "content": {"type": "text", "text": "x"}, "model": "m"},
        {"role": "assistant", "content": {"type": "audio", "data": "AAAA"}, "model": "m"},
        {"role": "assistant", "model": "m"},
    ],
)
async def test_sampling_checks_the_reply(mcp: McpClient, reply: JsonObject) -> None:
    await mcp.open(capabilities={"sampling": {}}, replies={"sampling/createMessage": reply})
    result = await mcp.call_tool("trigger-sampling-request", {"prompt": "Check me"})
    assert dig(result, "isError") is True
    assert text(result).startswith("[\n  {\n")


async def test_sampling_tool_arguments(mcp: McpClient) -> None:
    await mcp.open(capabilities={"sampling": {}}, replies={"sampling/createMessage": SAMPLED})
    result = await mcp.call_tool("trigger-sampling-request", {"maxTokens": 5})
    assert text(result).endswith("Invalid input: expected string, received undefined at prompt")


FORM_ANSWER: JsonObject = {
    "action": "accept",
    "content": {
        "name": "Grace Hopper",
        "check": True,
        "email": "grace@example.org",
        "birthdate": "1906-12-09",
        "integer": 64,
        "number": 0,
        "untitledMultipleSelectEnum": ["Drums"],
    },
}


async def test_form_elicitation_accepted(mcp: McpClient) -> None:
    await mcp.open(capabilities={"elicitation": {}}, replies={"elicitation/create": FORM_ANSWER})
    result = await mcp.call_tool("trigger-elicitation-request", {})
    assert texts(result)[:2] == [
        "✅ User provided the requested information!",
        (
            "User inputs:\n- Name: Grace Hopper\n- Agreed to terms: true\n- Email: grace@example.org\n"
            "- Birthdate: 1906-12-09\n- Favorite Integer: 64\n- Favorite Number: 0"
        ),
    ]
    assert texts(result)[2].startswith('\nRaw result: {\n  "action": "accept",')


@pytest.mark.parametrize(
    ("reply", "first"),
    [
        ({"action": "decline"}, "❌ User declined to provide the requested information."),
        ({"action": "cancel"}, "⚠️ User cancelled the elicitation dialog."),
        (
            {"action": "accept", "content": {}},
            "✅ User provided the requested information!",
        ),  # {} is truthy
    ],
)
async def test_form_elicitation_other_answers(
    mcp: McpClient, reply: JsonObject, first: str
) -> None:
    await mcp.open(capabilities={"elicitation": {}}, replies={"elicitation/create": reply})
    result = await mcp.call_tool("trigger-elicitation-request", {})
    assert texts(result)[0] == first


@pytest.mark.parametrize(
    "reply",
    [
        {"action": "skip"},
        {"action": "accept", "content": {"name": ["a", 1]}},
        {"content": {"x": "y"}},
    ],
)
async def test_form_elicitation_checks_the_reply(mcp: McpClient, reply: JsonObject) -> None:
    await mcp.open(capabilities={"elicitation": {}}, replies={"elicitation/create": reply})
    result = await mcp.call_tool("trigger-elicitation-request", {})
    assert dig(result, "isError") is True


URL_CAPABLE: JsonObject = {"elicitation": {"form": {}, "url": {}}}


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (
            "accept",
            "✅ User completed the URL elicitation flow.\nElicitation ID: scenario-1\nURL: https://auth.example.net/consent?client=7",
        ),
        ("decline", "❌ User declined to open the URL (Elicitation ID: scenario-1)."),
        ("cancel", "⚠️ User cancelled the URL elicitation (Elicitation ID: scenario-1)."),
    ],
)
async def test_url_elicitation(mcp: McpClient, action: str, expected: str) -> None:
    await mcp.open(capabilities=URL_CAPABLE, replies={"elicitation/create": {"action": action}})
    result = await mcp.call_tool(
        "trigger-url-elicitation",
        {
            "url": "https://auth.example.net/consent?client=7",
            "elicitationId": "scenario-1",
            "message": "Approve",
        },
    )
    assert texts(result)[0] == expected


async def test_url_elicitation_by_error_then_retry(mcp: McpClient) -> None:
    await mcp.open(capabilities=URL_CAPABLE, replies={"elicitation/create": {"action": "accept"}})
    arguments: JsonObject = {"url": "https://sso.example.com/start", "errorPath": True}
    required = await mcp.call_tool(
        "trigger-url-elicitation",
        arguments,
        compare=varying("$.data.elicitations[*].elicitationId"),
    )
    assert dig(required, "code") == -32042
    assert (
        dig(required, "message")
        == "MCP error -32042: This request requires browser-based authorization."
    )
    prerequisite = dig(required, "data", "elicitations", 0)
    assert dig(prerequisite, "url") == "https://modelcontextprotocol.io"
    assert UUID.match(str(dig(prerequisite, "elicitationId")))
    retried = await mcp.call_tool("trigger-url-elicitation", arguments, compare=TEXTS)
    assert texts(retried)[0].startswith(
        "✅ User completed the URL elicitation flow.\nElicitation ID: "
    )
    third = await mcp.call_tool(
        "trigger-url-elicitation",
        arguments,
        compare=varying("$.data.elicitations[*].elicitationId"),
    )
    assert dig(third, "code") == -32042


async def test_url_elicitation_needs_a_url(mcp: McpClient) -> None:
    await mcp.open(capabilities=URL_CAPABLE)
    result = await mcp.call_tool("trigger-url-elicitation", {"url": "sso dot example"})
    assert text(result).endswith("Invalid URL at url")


@pytest.mark.parametrize(
    ("reply", "clarification"),
    [
        ({"action": "accept", "content": {"interpretation": "comedy"}}, "comedy"),
        ({"action": "decline"}, "User declined - using default interpretation"),
    ],
)
async def test_ambiguous_research_asks_through_tasks_result(
    mcp: McpClient, reply: JsonObject, clarification: str
) -> None:
    await mcp.open(capabilities={"elicitation": {}}, replies={"elicitation/create": reply})
    created = await tool_call(
        mcp,
        {
            "name": "simulate-research-query",
            "arguments": {"topic": "Python", "ambiguous": True},
            "task": {},
        },
        compare=task_times("$.task"),
    )
    result = await mcp.request("tasks/result", {"taskId": dig(created, "task", "taskId")})
    report = str(dig(result, "content", 0, "text"))
    assert report.startswith(f"# Research Report: Python ({clarification})\n")
    assert "`input_required` → `working` → `completed`" in report


async def test_roots_without_a_get_stream(mcp: McpClient) -> None:
    """The server asks for roots outside the call; a client without a GET stream never hears it."""
    await mcp.open(
        capabilities={"roots": {"listChanged": True}},
        replies={"roots/list": {"roots": [{"uri": "file:///repo", "name": "repo"}]}},
    )
    result = await mcp.call_tool("get-roots-list", {})
    assert text(result).startswith(
        "The client supports roots but no roots are currently configured."
    )


ASYNC_CAPABLE: JsonObject = {
    "sampling": {},
    "elicitation": {},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


async def test_async_sampling_answered_at_once(mcp: McpClient) -> None:
    await mcp.open(capabilities=ASYNC_CAPABLE, replies={"sampling/createMessage": SAMPLED})
    result = await mcp.call_tool("trigger-sampling-request-async", {"prompt": "Quick one"})
    assert text(result).startswith(
        '[SYNC] Client executed synchronously:\n{\n  "role": "assistant",'
    )


async def test_async_sampling_through_a_client_task(mcp: McpClient) -> None:
    replies: JsonObject = {
        "sampling/createMessage": {
            "task": {"taskId": "ct-9", "status": "working", "pollInterval": 250}
        },
        "tasks/get": {"taskId": "ct-9", "status": "completed"},
        "tasks/result": {
            "model": "late-model",
            "role": "assistant",
            "content": {"type": "text", "text": "Done."},
        },
    }
    await mcp.open(capabilities=ASYNC_CAPABLE, replies=replies)
    result = await mcp.call_tool(
        "trigger-sampling-request-async", {"prompt": "Slow one", "maxTokens": 9}
    )
    assert text(result).startswith(
        '[COMPLETED] Async sampling completed!\n\n**Progress:**\nTask created: ct-9\nPoll 1: completed\n\n**Result:**\n{\n  "model": "late-model",'
    )


async def test_async_sampling_failed_client_task(mcp: McpClient) -> None:
    replies: JsonObject = {
        "sampling/createMessage": {"task": {"taskId": "ct-10", "status": "working"}},
        "tasks/get": {"status": "failed", "statusMessage": "quota exceeded"},
    }
    await mcp.open(capabilities=ASYNC_CAPABLE, replies=replies)
    result = await mcp.call_tool("trigger-sampling-request-async", {"prompt": "Doomed"})
    assert (
        text(result)
        == "[FAILED] quota exceeded\n\nProgress:\nTask created: ct-10\nPoll 1: failed - quota exceeded"
    )


async def test_async_elicitation(mcp: McpClient) -> None:
    replies: JsonObject = {
        "elicitation/create": {"task": {"taskId": "ct-11", "status": "input_required"}},
        "tasks/get": {"status": "completed", "statusMessage": "answered"},
        "tasks/result": {"action": "accept", "content": {"name": "Lin", "favoriteColor": "Purple"}},
    }
    await mcp.open(capabilities=ASYNC_CAPABLE, replies=replies)
    result = await mcp.call_tool("trigger-elicitation-request-async", {})
    assert texts(result)[:2] == [
        "[COMPLETED] User provided the requested information!",
        "User inputs:\n- Name: Lin\n- Favorite Color: Purple",
    ]
    assert texts(result)[2].startswith(
        "\nProgress:\nTask created: ct-11\nPoll 1: completed - answered\n\nRaw result: "
    )


async def test_async_elicitation_answered_at_once(mcp: McpClient) -> None:
    await mcp.open(
        capabilities=ASYNC_CAPABLE, replies={"elicitation/create": {"action": "decline"}}
    )
    result = await mcp.call_tool("trigger-elicitation-request-async", {})
    assert text(result) == '[SYNC] Client executed synchronously:\n{\n  "action": "decline"\n}'


@pytest.mark.parametrize(
    ("capabilities", "extra"),
    [
        ({"sampling": {}}, ["trigger-sampling-request"]),
        ({"elicitation": {}}, ["trigger-elicitation-request"]),
        ({"elicitation": {"url": {}}}, ["trigger-elicitation-request", "trigger-url-elicitation"]),
        ({"roots": {}}, ["get-roots-list"]),
        (
            {"sampling": {}, "tasks": {"requests": {"sampling": {"createMessage": {}}}}},
            ["trigger-sampling-request", "trigger-sampling-request-async"],
        ),
        (
            {"elicitation": {}, "tasks": {"requests": {"elicitation": {"create": {}}}}},
            ["trigger-elicitation-request", "trigger-elicitation-request-async"],
        ),
        ({"tasks": {"requests": {"sampling": {"createMessage": {}}}}}, []),
    ],
)
async def test_tools_offered_by_capability(
    mcp: McpClient, capabilities: JsonObject, extra: list[str]
) -> None:
    await mcp.open(capabilities=capabilities)
    listed = await mcp.list_tools()
    names = [str(dig(t, "name")) for t in dig(listed, "tools")]  # type: ignore[union-attr]
    assert [n for n in names if n not in PLAIN_TOOLS] == extra


async def test_capability_tools_are_unknown_to_others(mcp: McpClient) -> None:
    await mcp.open(capabilities={"sampling": {}})
    hidden = await mcp.call_tool("trigger-elicitation-request", {})
    assert text(hidden) == "MCP error -32602: Tool trigger-elicitation-request not found"
    roots = await mcp.call_tool("get-roots-list", {})
    assert text(roots) == "MCP error -32602: Tool get-roots-list not found"
