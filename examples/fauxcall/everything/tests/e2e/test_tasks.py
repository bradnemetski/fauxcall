"""``simulate-research-query`` and its tasks: stages, polling, results, cancellation, clarification.

Polls happen mid-stage (stages last one second from the task's creation), so both servers are
asked the same number of times and answer the same status.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from everything_support import LISTED_TASK_TIMES, TASK_TIMES
from fauxcall.testing import CompareOptions

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject

TOOL = "simulate-research-query"
TIMES = CompareOptions(policies=list(TASK_TIMES))
LISTED = CompareOptions(policies=list(LISTED_TASK_TIMES))


async def _start(mcp: McpClient, arguments: JsonObject, task: JsonObject | None = None) -> str:
    created: Any = await mcp.request(
        "tools/call",
        {
            "name": TOOL,
            "arguments": arguments,
            "task": task if task is not None else {"ttl": 60000},
        },
        compare=TIMES,
    )
    assert isinstance(created, dict)
    assert created["task"]["status"] == "working"
    return str(created["task"]["taskId"])


async def _get(mcp: McpClient, task_id: str) -> JsonObject:
    task: Any = await mcp.request("tasks/get", {"taskId": task_id}, compare=TIMES)
    assert isinstance(task, dict)
    return task


async def test_research_runs_through_its_stages(mcp: McpClient) -> None:
    await mcp.listen()
    task_id: Any = await _start(mcp, {"topic": "coral reef ecology"})
    seen: list[JsonObject] = []
    for _ in range(5):
        await asyncio.sleep(1.0 if seen else 0.5)
        seen.append(await _get(mcp, task_id))
    assert [task["status"] for task in seen] == ["working"] * 4 + ["completed"]
    assert seen[1]["statusMessage"] == "Analyzing content..."
    result: Any = await mcp.request("tasks/result", {"taskId": task_id})
    assert isinstance(result, dict)
    assert result["content"][0]["text"].startswith("# Research Report: coral reef ecology")
    statuses: Any = await mcp.heard("notifications/tasks/status", count=5, within=3)
    assert [(m["params"]["status"], m["params"]["statusMessage"]) for m in statuses] == [
        ("working", "Gathering sources..."),
        ("working", "Analyzing content..."),
        ("working", "Synthesizing findings..."),
        ("working", "Generating report..."),
        ("completed", "Generating report..."),
    ]


async def test_result_waits_for_the_task(mcp: McpClient) -> None:
    task_id: Any = await _start(mcp, {"topic": "history of cartography", "ambiguous": True})
    result: Any = await mcp.request("tasks/result", {"taskId": task_id})
    assert isinstance(result, dict)
    assert "Clarification" not in result["content"][0]["text"]
    listing: Any = await mcp.request("tasks/list", {}, compare=LISTED)
    assert isinstance(listing, dict)
    assert len(listing["tasks"]) == 1


async def test_cancel_a_running_task(mcp: McpClient) -> None:
    task_id: Any = await _start(mcp, {"topic": "tidal energy"})
    await asyncio.sleep(0.5)
    cancelled: Any = await mcp.request("tasks/cancel", {"taskId": task_id}, compare=TIMES)
    assert isinstance(cancelled, dict)
    assert cancelled["status"] == "cancelled"
    await asyncio.sleep(1.0)
    assert (await _get(mcp, task_id))["status"] == "cancelled"
    await mcp.request("tasks/result", {"taskId": task_id})
    again: Any = await mcp.request("tasks/cancel", {"taskId": task_id})
    assert isinstance(again, dict)
    assert again["code"] == -32602


async def test_task_errors(mcp: McpClient) -> None:
    await mcp.request("tasks/get", {"taskId": "ffffffffffffffffffffffffffffffff"})
    await mcp.request("tasks/result", {"taskId": "missing"})
    await mcp.request("tasks/cancel", {"taskId": "missing"})
    await mcp.request("tasks/get", {})
    await mcp.request("tasks/list", {"cursor": "not-a-task"})
    assert await mcp.request("tasks/list", {}) == {"tasks": [], "_meta": {}}


async def test_research_needs_a_task(mcp: McpClient) -> None:
    plain: Any = await mcp.call_tool(TOOL, {"topic": "anything"})
    assert isinstance(plain, dict)
    assert plain["isError"] is True
    refused: Any = await mcp.request(
        "tools/call", {"name": TOOL, "arguments": {"ambiguous": "yes"}, "task": {}}
    )
    assert isinstance(refused, dict)
    assert refused["code"] == -32602


async def test_ambiguous_research_asks_for_clarification(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {}},
        replies={
            "elicitation/create": {"action": "accept", "content": {"interpretation": "comedy"}}
        },
    )
    await mcp.listen()
    task_id: Any = await _start(mcp, {"topic": "Monty Python", "ambiguous": True})
    await asyncio.sleep(2.5)
    assert (await _get(mcp, task_id))["status"] == "input_required"
    result: Any = await mcp.request("tasks/result", {"taskId": task_id})
    assert isinstance(result, dict)
    assert "(comedy)" in result["content"][0]["text"]
    statuses: Any = await mcp.heard("notifications/tasks/status", count=8, within=3)
    assert [m["params"]["status"] for m in statuses] == [
        "working",
        "working",
        "working",
        "input_required",
        "input_required",
        "working",
        "working",
        "completed",
    ]
    assert statuses[5]["params"]["statusMessage"] == 'Continuing with interpretation: "comedy"...'


async def test_declined_clarification(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {}}, replies={"elicitation/create": {"action": "decline"}}
    )
    task_id: Any = await _start(mcp, {"topic": "jupiter", "ambiguous": True})
    result: Any = await mcp.request("tasks/result", {"taskId": task_id})
    assert isinstance(result, dict)
    assert "User declined" in result["content"][0]["text"]


async def test_cancel_while_waiting_for_clarification(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {}}, replies={"elicitation/create": {"action": "accept"}}
    )
    task_id = await _start(mcp, {"topic": "python", "ambiguous": True})
    await asyncio.sleep(2.5)
    assert (await _get(mcp, task_id))["status"] == "input_required"
    cancelled = await mcp.request("tasks/cancel", {"taskId": task_id}, compare=TIMES)
    assert isinstance(cancelled, dict)
    error = await mcp.request("tasks/result", {"taskId": task_id})
    assert isinstance(error, dict)
    assert error["code"] == -32603
    assert (await _get(mcp, task_id))["status"] == "cancelled"
