"""Prompts, completions and protocol refusals the recordings don't cover one by one."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from mcp_session import session

if TYPE_CHECKING:
    from fauxcall.types import JsonObject

COMPLETABLE: JsonObject = {"type": "ref/prompt", "name": "completable-prompt"}


async def test_prompt_arguments_must_be_strings() -> None:
    async with session() as client:
        listed = await client.call("prompts/get", {"name": "args-prompt", "arguments": ["city"]})
        assert listed["code"] == -32603
        assert "expected record, received array" in listed["message"]
        number = await client.call("prompts/get", {"name": "args-prompt", "arguments": {"city": 1}})
        assert '"arguments",\n      "city"' in number["message"]
        unnamed = await client.call("prompts/get", {"name": None})
        assert "expected string, received null" in unnamed["message"]


async def test_prompts_without_arguments() -> None:
    async with session() as client:
        missing = await client.call("prompts/get", {"name": "completable-prompt"})
        assert missing["message"] == (
            "MCP error -32602: Invalid arguments for prompt completable-prompt: Invalid input: "
            "expected string, received undefined at department\n"
            "Invalid input: expected string, received undefined at name"
        )


@pytest.mark.parametrize(
    "params",
    [
        {"ref": COMPLETABLE, "argument": {"name": 1, "value": "x"}},
        {"ref": COMPLETABLE, "argument": {"name": "department", "value": None}},
        {"ref": COMPLETABLE, "argument": "department"},
    ],
)
async def test_completion_arguments_refused_by_the_schema(params: Any) -> None:
    async with session() as client:
        error = await client.call("completion/complete", params)
        assert error["code"] == -32603


async def test_completions_cap_at_one_hundred() -> None:
    async with session() as client:
        result = await client.call(
            "completion/complete",
            {
                "ref": COMPLETABLE,
                "argument": {"name": "name", "value": ""},
                "context": {"arguments": {"department": "Support"}},
            },
        )
        assert result == {
            "completion": {"values": ["John", "Kim", "Lee"], "total": 3, "hasMore": False}
        }
        odd_context = await client.call(
            "completion/complete",
            {"ref": COMPLETABLE, "argument": {"name": "name", "value": ""}, "context": "x"},
        )
        assert odd_context["code"] == -32603  # 000503
        assert '"params",\n      "context"' in odd_context["message"]


async def test_unknown_completion_reference_types_answer_as_recorded() -> None:
    async with session() as client:
        error = await client.call(
            "completion/complete",
            {
                "ref": {"type": "ref/tool", "name": "echo"},
                "argument": {"name": "message", "value": "h"},
            },
        )
        assert error["code"] == -32603
        assert '"invalid_union"' in error["message"]


async def test_tool_calls_refused_by_the_schema() -> None:
    async with session() as client:
        named = await client.call("tools/call", {"name": 5})
        assert "expected string, received number" in named["message"]
        listed = await client.call("tools/call", {"name": "echo", "arguments": [1]})
        assert "expected record, received array" in listed["message"]
        nulled = await client.call("tools/call", {"name": "echo", "arguments": None})
        assert "expected record, received null" in nulled["message"]


async def test_task_augmented_plain_tools_still_run() -> None:
    async with session(listen=True) as client:
        error = await client.tool(
            "gzip-file-as-resource", {"name": "t.gz", "data": "data:,x"}, task={"ttl": 10}
        )
        assert error["message"].startswith("MCP error -32602: Invalid task creation result")
        listed = await client.call("resources/list", {})
        assert listed["resources"][-1]["name"] == "t.gz"
        assert await client.heard("notifications/resources/list_changed")
        unaugmented = await client.tool("echo", {"message": "m"}, task=None)
        assert unaugmented == {"content": [{"type": "text", "text": "Echo: m"}]}
        falsy = await client.tool("echo", {"message": "m"}, task=0)
        assert falsy == unaugmented
