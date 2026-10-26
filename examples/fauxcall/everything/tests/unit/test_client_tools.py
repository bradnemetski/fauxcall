"""Tools that ask the client: roots syncing, async task polling, malformed client answers.

The recorded sessions themselves are replayed in ``test_sessions.py``; these cover what a
recording can't show at once (timeouts, many polls, answers the SDK refuses).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from mcp_session import session, texts

from fauxcall_everything.client_tools import (
    ResultError,
    parse_elicit_result,
    parse_sampling_result,
    truthy,
)

if TYPE_CHECKING:
    from fauxcall.types import JsonObject

TASKS: JsonObject = {
    "sampling": {},
    "elicitation": {},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}
ROOTS = {"roots": [{"uri": "file:///work", "name": "work"}]}


async def test_roots_are_asked_350_ms_after_initialization() -> None:
    async with session(
        {"roots": {"listChanged": True}}, {"roots/list": ROOTS}, listen=True
    ) as client:
        asked = await client.heard("roots/list")
        assert asked[0]["id"] == 0
        log = await client.heard("notifications/message")
        assert log[0]["params"] == {
            "level": "info",
            "logger": "everything-server",
            "data": "Roots updated: 1 root(s) received from client",
        }
        result = await client.tool("get-roots-list")
        assert texts(result)[0].startswith(
            "Current MCP Roots (1 total):\n\n1. work\n   URI: file:///work"
        )
        client.answer({"roots/list": {"roots": []}})
        await client.wire.notify("notifications/roots/list_changed")
        await client.heard("roots/list")
        emptied = await client.heard("notifications/message")
        assert emptied[0]["params"]["data"] == "Roots updated: 0 root(s) received from client"
        after = await client.tool("get-roots-list")
        assert texts(after)[0].startswith("The client supports roots but no roots")


async def test_roots_answers_the_sdk_refuses_or_lacks() -> None:
    async with session({"roots": {}}, {"roots/list": {}}) as client:
        await asyncio.sleep(0.5)
        await client.wire.listen()
        result = await client.tool("get-roots-list")
        assert texts(result)[0].startswith("The client supports roots but no roots")
        assert await client.heard("notifications/message", within=0.3) == []  # 000494-000495
        client.answer({"roots/list": {"roots": [{"uri": "https://not-a-file"}]}})
        refused = await client.tool("get-roots-list")
        assert texts(refused)[0].startswith("The client supports roots but no roots")


async def test_roots_unanswered_without_a_stream() -> None:
    async with session({"roots": {}}, {"roots/list": ROOTS}) as client:
        result = await client.tool("get-roots-list")
        assert texts(result)[0].startswith("The client supports roots but no roots")


async def test_async_sampling_gives_up_after_60_polls() -> None:
    async with session(TASKS) as client:
        client.answer(
            {
                "sampling/createMessage": {"task": {"taskId": "t", "status": "working"}},
                "tasks/get": {"status": "working", "statusMessage": "still"},
            }
        )
        result = await client.tool("trigger-sampling-request-async", {"prompt": "slow"})
        text = texts(result)[0]
        assert text.startswith("[TIMEOUT] Task timed out after 60 poll attempts\n\nProgress:\n")
        assert text.endswith("Poll 60: working - still")


async def test_async_elicitation_logs_input_required_sparingly() -> None:
    polls = 0

    async def reply(request: JsonObject) -> Any:
        nonlocal polls
        method = request["method"]
        if method == "elicitation/create":
            result: JsonObject = {"task": {"taskId": "e", "status": "input_required"}}
        elif method == "tasks/get":
            polls += 1
            result = {"status": "input_required" if polls < 25 else "completed"}
        else:
            result = {"action": "accept", "content": {"name": "N", "agreeToTerms": False}}
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    async with session(TASKS) as client:
        client.answer(reply)
        result = await client.tool("trigger-elicitation-request-async")
        progress = texts(result)[2]
        logged = [line for line in progress.splitlines() if line.startswith("Poll")]
        assert logged == [
            "Poll 1: input_required",
            "Poll 10: input_required",
            "Poll 20: input_required",
            "Poll 25: completed",
        ]
        assert texts(result)[1] == "User inputs:\n- Name: N\n- Agreed to terms: false"


async def test_async_tools_refuse_answers_of_neither_kind() -> None:
    async with session(TASKS, {"sampling/createMessage": {}, "elicitation/create": {}}) as client:
        sampling = await client.tool("trigger-sampling-request-async", {"prompt": "?"})
        assert sampling["isError"] is True
        elicitation = await client.tool("trigger-elicitation-request-async")
        assert elicitation["isError"] is True


async def test_async_elicitation_completed_without_a_known_action() -> None:
    replies = {
        "elicitation/create": {"task": {"taskId": "e", "status": "completed"}},
        "tasks/result": {"action": "other"},
    }
    async with session(TASKS, replies) as client:
        result = await client.tool("trigger-elicitation-request-async")
        assert texts(result) == [
            '\nProgress:\nTask created: e\n\nRaw result: {\n  "action": "other"\n}'
        ]


async def test_async_poll_answers_the_sdk_refuses() -> None:
    replies = {
        "sampling/createMessage": {"task": {"taskId": "t", "status": "working"}},
        "tasks/get": {"statusMessage": "no status"},
    }
    async with session(TASKS, replies) as client:
        result = await client.tool("trigger-sampling-request-async", {"prompt": "x"})
        assert result["isError"] is True


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("text", "expected object, received string"),
        (
            {"role": "robot", "model": "m", "content": {"type": "text", "text": "t"}},
            '"invalid_value"',
        ),
        (
            {"role": "assistant", "model": "m", "content": {"type": "text"}},
            '"content",\n      "text"',
        ),
        (
            {"role": "assistant", "model": "m", "content": {"type": "image", "data": "AA=="}},
            '"mimeType"',
        ),
        ({"role": "assistant", "model": "m", "content": "x"}, "expected object, received string"),
    ],
)
def test_sampling_answers_the_sdk_refuses(answer: Any, message: str) -> None:
    with pytest.raises(ResultError) as caught:
        parse_sampling_result(answer)
    assert message in str(caught.value)


def test_sampling_answers_keep_unknown_members_last() -> None:
    parsed = parse_sampling_result(
        {
            "extra": 1,
            "content": {"text": "t", "type": "text", "junk": 2},
            "role": "user",
            "model": "m",
        }
    )
    assert list(parsed) == ["model", "role", "content", "extra"]
    assert parsed["content"] == {"type": "text", "text": "t"}


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (None, "expected object, received null"),
        ({"action": "accept", "content": "x"}, "expected record, received string"),
        ({"action": "accept", "content": {"tags": ["a", 3]}}, '"path": [\n            1'),
        ({"action": "accept", "content": {"n": None}}, "expected string, received null"),
    ],
)
def test_elicitation_answers_the_sdk_refuses(answer: Any, message: str) -> None:
    with pytest.raises(ResultError) as caught:
        parse_elicit_result(answer)
    assert message in str(caught.value)


def test_elicitation_answers_drop_null_content() -> None:
    assert parse_elicit_result({"content": None, "action": "cancel", "_meta": {}}) == {
        "_meta": {},
        "action": "cancel",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        (False, False),
        ("", False),
        (0, False),
        (0.0, False),
        ("x", True),
        (1, True),
        ({}, True),
    ],
)
def test_truthiness(value: Any, expected: bool) -> None:
    assert truthy(value) is expected
