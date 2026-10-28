"""``simulate-research-query`` and the ``tasks/*`` methods: the task lifecycle end to end."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from fauxcall.model.fields import RandomPolicy
from fauxcall.testing import CompareOptions
from scenario_support import assert_task, task_compare, task_policies, text_of

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonValue

RESEARCH = "simulate-research-query"
RELATED = "$._meta['io.modelcontextprotocol/related-task'].taskId"
RESULT = CompareOptions(
    policies=[RandomPolicy(path=RELATED, scope="body", generator="hex", length=32)]
)


async def start(mcp: McpClient, topic: str, **arguments: JsonValue) -> str:
    created: Any = await mcp.request(
        "tools/call",
        {"name": RESEARCH, "arguments": {"topic": topic, **arguments}, "task": {"ttl": 90000}},
        compare=task_compare("$.task"),
    )
    assert isinstance(created, dict), created
    task = assert_task(created["task"], "working")
    assert task["statusMessage"] == "Gathering sources..."
    return str(task["taskId"])


async def test_research_needs_a_task(mcp: McpClient) -> None:
    refused: Any = await mcp.call_tool(RESEARCH, {"topic": "tidal power"})
    assert text_of(refused).startswith("MCP error -32601: Tool simulate-research-query requires")


async def test_research_task_lifecycle(mcp: McpClient) -> None:
    await mcp.listen()
    task_id: Any = await start(mcp, "tidal power")
    early: Any = await mcp.request("tasks/get", {"taskId": task_id}, compare=task_compare())
    assert_task(early, "working")
    report: Any = await mcp.request("tasks/result", {"taskId": task_id}, compare=RESULT)
    assert text_of(report).startswith("# Research Report: tidal power\n")
    done: Any = await mcp.request("tasks/get", {"taskId": task_id}, compare=task_compare())
    assert assert_task(done, "completed")["statusMessage"] == "Generating report..."
    statuses: Any = await mcp.heard("notifications/tasks/status", count=5, within=8)
    assert [m["params"]["status"] for m in statuses if isinstance(m, dict)][-1] == "completed"
    listed: Any = await mcp.request(
        "tasks/list", {}, compare=CompareOptions(policies=list(task_policies("$.tasks[*]")))
    )
    assert isinstance(listed, dict)
    assert [t["taskId"] for t in listed["tasks"]] == [task_id]
    again: Any = await mcp.request("tasks/result", {"taskId": task_id}, compare=RESULT)
    assert text_of(again) == text_of(report)
    refused: Any = await mcp.request("tasks/cancel", {"taskId": task_id})
    assert refused == {
        "code": -32602,
        "message": "MCP error -32602: Cannot cancel task in terminal status: completed",
    }


async def test_cancelling_a_research_task(mcp: McpClient) -> None:
    task_id: Any = await start(mcp, "history of cartography")
    cancelled: Any = await mcp.request("tasks/cancel", {"taskId": task_id}, compare=task_compare())
    assert isinstance(cancelled, dict)
    assert next(iter(cancelled)) == "_meta"
    assert cancelled["status"] == "cancelled"
    assert cancelled["statusMessage"] == "Client cancelled task execution."
    missing: Any = await mcp.request(
        "tasks/result", {"taskId": task_id}, compare=CompareOptions(values=False)
    )
    assert isinstance(missing, dict)
    assert missing["message"] == f"Task {task_id} has no result stored"
    await mcp.request("tasks/cancel", {"taskId": task_id})
    after: Any = await mcp.request("tasks/get", {"taskId": task_id}, compare=task_compare())
    assert assert_task(after, "cancelled")


async def test_task_errors(mcp: McpClient) -> None:
    for method in ("tasks/get", "tasks/result", "tasks/cancel"):
        await mcp.request(method, {"taskId": "no-such-task"})
    await mcp.request("tasks/get", {"taskId": 5})
    await mcp.request("tasks/list", {"cursor": "nope"})
    await mcp.request("tasks/list", {"cursor": 5})
    for name in ("no-such-tool", "trigger-sampling-request"):  # not there: refused as tasks
        refused: Any = await mcp.request("tools/call", {"name": name, "arguments": {}, "task": {}})
        assert refused["message"].startswith("MCP error -32602: Invalid task creation result")
    empty: Any = await mcp.request("tasks/list", {})
    assert empty == {"tasks": [], "_meta": {}}
    invalid: Any = await mcp.request(
        "tools/call", {"name": RESEARCH, "arguments": {"ambiguous": True}, "task": {}}
    )
    assert isinstance(invalid, dict)
    assert invalid["message"].startswith("MCP error -32602: Invalid task creation result")


async def test_ambiguous_research_asks_for_clarification(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {}},
        replies={
            "elicitation/create": {"action": "accept", "content": {"interpretation": "historical"}}
        },
    )
    task_id: Any = await start(mcp, "solar sails", ambiguous=True)
    started = time.monotonic()
    report: Any = await mcp.request(
        "tasks/result", {"taskId": task_id}, compare=RESULT, notifications=True
    )
    # asked at the 2 s tick, then two 1 s stages: answered on the next tick (000301: 5024 ms)
    assert 3.8 <= time.monotonic() - started < 7.0
    text = text_of(report)
    assert text.startswith("# Research Report: solar sails (historical)\n")
    assert '("historical"), the task resumed processing and completed.' in text


async def test_plain_tools_called_as_tasks_run_then_fail(mcp: McpClient) -> None:
    echoed: Any = await mcp.request(
        "tools/call", {"name": "echo", "arguments": {"message": "hi"}, "task": {"ttl": 5000}}
    )
    assert echoed["code"] == -32602
    assert echoed["message"].startswith("MCP error -32602: Invalid task creation result: [")
    await mcp.request("tools/call", {"name": "get-sum", "arguments": {"a": 1}, "task": {}})
    started: Any = await mcp.request(
        "tools/call", {"name": "toggle-simulated-logging", "arguments": {}, "task": {}}
    )
    assert started["code"] == -32602
    stopped: Any = await mcp.call_tool("toggle-simulated-logging", {})
    assert text_of(stopped).startswith("Stopped simulated logging")  # the refused call still ran
    shaped: Any = await mcp.request(
        "tools/call", {"name": "echo", "arguments": {"message": "x"}, "task": True}
    )
    assert shaped["code"] == -32603
