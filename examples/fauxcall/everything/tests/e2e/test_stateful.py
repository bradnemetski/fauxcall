"""Scenarios with state: gzip session resources, toggles, research tasks, client callbacks.

These tools may modify state, so on the real server they need ``--allow-real-writes`` and spend
the approved budget (``data/approvals.json``): each scenario makes as few such calls as it can.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
from typing import TYPE_CHECKING, Any, Final

import pytest
from fauxcall.model.fields import NowPolicy
from fauxcall.testing.client import CompareOptions
from fauxcall.timefmt import TimeFormat

from fauxcall_everything.tasks import REPORT, STAGES

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

ISO_MS: Final = TimeFormat(kind="iso", fraction_digits=3, tz_style="Z")


def task_times(*prefixes: str) -> CompareOptions:
    """Task timestamps track the clock: compared by behavior (close to each side's now)."""
    return CompareOptions(
        policies=[
            NowPolicy(path=f"{prefix}{name}", scope="body", format=ISO_MS)
            for prefix in prefixes
            for name in ("createdAt", "lastUpdatedAt")
        ]
    )


def data_url(content: bytes) -> str:
    return "data:text/plain;base64," + base64.b64encode(content).decode()


def unzipped(blob: str) -> bytes:
    return gzip.decompress(base64.b64decode(blob))


# -- gzip-file-as-resource -----------------------------------------------------------------------


async def test_gzip_registers_a_session_resource(mcp: McpClient) -> None:
    first: Any = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "e2e-notes.txt.gz", "data": data_url(b"first draft")}
    )
    link = first["content"][0]
    assert link == {
        "name": "e2e-notes.txt.gz",
        "uri": "demo://resource/session/e2e-notes.txt.gz",
        "mimeType": "application/gzip",
        "type": "resource_link",
    }
    read: Any = await mcp.request("resources/read", {"uri": link["uri"]})
    assert unzipped(read["contents"][0]["blob"]) == b"first draft"

    inline: Any = await mcp.call_tool(
        "gzip-file-as-resource",
        {
            "name": "e2e-notes.txt.gz",
            "data": "data:,final%20version%20%E2%9C%93#not-in-the-file",
            "outputType": "resource",
        },
    )
    resource = inline["content"][0]["resource"]
    assert unzipped(resource["blob"]) == "final version ✓".encode()
    again: Any = await mcp.request("resources/read", {"uri": link["uri"]})
    assert again["contents"][0]["blob"] == resource["blob"]  # the name now holds the new file

    listed: Any = await mcp.request("resources/list", {})
    uris = [r["uri"] for r in listed["resources"]]
    assert uris[0] == "demo://resource/static/document/architecture.md"


async def test_gzip_refusals(mcp: McpClient) -> None:
    invalid: Any = await mcp.call_tool("gzip-file-as-resource", {"name": "x.gz", "data": "nope"})
    assert invalid["content"][0]["text"].endswith("Invalid URL at data")
    unreachable: Any = await mcp.call_tool(
        "gzip-file-as-resource",
        {"name": "x.gz", "data": "https://fauxcall-e2e.invalid/missing.txt"},
    )
    assert unreachable == {"content": [{"type": "text", "text": "fetch failed"}], "isError": True}
    missing: Any = await mcp.request("resources/read", {"uri": "demo://resource/session/x.gz"})
    assert missing["code"] == -32602


# -- toggles -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "started", "stopped"),
    [
        (
            "toggle-simulated-logging",
            "Started simulated, random-leveled logging for session {sid} at a 5 second pace.",
            "Stopped simulated logging for session {sid}",
        ),
        (
            "toggle-subscriber-updates",
            "Started simulated resource updated notifications for session {sid} at a 5 second",
            "Stopped simulated resource updates for session {sid}",
        ),
    ],
)
async def test_toggles_alternate_per_session(
    mcp: McpClient, tool: str, started: str, stopped: str
) -> None:
    await mcp.request("ping")
    sid = mcp.wire.session_id
    on: Any = await mcp.call_tool(tool, {})
    assert on["content"][0]["text"].startswith(started.format(sid=sid))
    off: Any = await mcp.call_tool(tool, {})
    assert off["content"][0]["text"] == stopped.format(sid=sid)


# -- research tasks ------------------------------------------------------------------------------


async def test_research_task_lifecycle(mcp: McpClient, call_extended: Any) -> None:
    topic = "history of tide clocks"
    created: Any = await call_extended(
        mcp,
        "simulate-research-query",
        {"topic": topic},
        task={"ttl": 45000},
        compare=task_times("task."),
    )
    task = created["task"]
    assert (task["status"], task["ttl"], task["pollInterval"]) == ("working", 300000, 1000)
    assert task["statusMessage"] == STAGES[0]
    seen = []
    for _ in range(20):
        status: Any = await mcp.request(
            "tasks/get", {"taskId": task["taskId"]}, compare=task_times("")
        )
        seen.append(status["statusMessage"])
        if status["status"] == "completed":
            break
        await asyncio.sleep(0.5)
    assert status["status"] == "completed"
    assert [message for message in STAGES if message in seen][-1] == STAGES[-1]
    result: Any = await mcp.request("tasks/result", {"taskId": task["taskId"]})
    assert result["content"][0]["text"] == REPORT.format(topic=topic)
    assert result["_meta"] == {"io.modelcontextprotocol/related-task": {"taskId": task["taskId"]}}
    listed: Any = await mcp.request("tasks/list", {}, compare=task_times("tasks[*]."))
    assert [t["taskId"] for t in listed["tasks"]] == [task["taskId"]]
    late: Any = await mcp.request("tasks/cancel", {"taskId": task["taskId"]})
    assert late["message"] == "MCP error -32602: Cannot cancel task in terminal status: completed"


async def test_research_task_cancelled_midway(mcp: McpClient, call_extended: Any) -> None:
    created: Any = await call_extended(
        mcp,
        "simulate-research-query",
        {"topic": "never mind"},
        task={},
        compare=task_times("task."),
    )
    task_id = created["task"]["taskId"]
    cancelled: Any = await mcp.request("tasks/cancel", {"taskId": task_id}, compare=task_times(""))
    assert cancelled["_meta"] == {}
    assert (cancelled["status"], cancelled["statusMessage"]) == (
        "cancelled",
        "Client cancelled task execution.",
    )
    status: Any = await mcp.request("tasks/get", {"taskId": task_id}, compare=task_times(""))
    assert status["status"] == "cancelled"
    result: Any = await mcp.request("tasks/result", {"taskId": task_id})
    assert result == {"code": -32603, "message": f"Task {task_id} has no result stored"}


async def test_research_needs_a_task_and_valid_arguments(
    mcp: McpClient, call_extended: Any
) -> None:
    plain: Any = await mcp.call_tool("simulate-research-query", {"topic": "anything"})
    assert plain["isError"] is True
    assert "requires task augmentation" in plain["content"][0]["text"]
    invalid: Any = await call_extended(mcp, "simulate-research-query", {"topic": 7}, task={})
    assert invalid["code"] == -32602
    assert invalid["message"].startswith("MCP error -32602: Invalid task creation result:")


async def test_tools_without_task_support_refuse_a_task(mcp: McpClient, call_extended: Any) -> None:
    answer: Any = await call_extended(mcp, "get-sum", {"a": 1, "b": 1}, task={"ttl": 1000})
    assert answer["code"] == -32602
    assert "Invalid task creation result" in answer["message"]


async def test_a_null_task_is_refused(mcp: McpClient) -> None:
    answer: Any = await mcp.request(
        "tools/call", {"name": "get-sum", "arguments": {"a": 1, "b": 2}, "task": None}
    )
    assert answer["code"] == -32603
    assert "expected object, received null" in answer["message"]


async def test_ambiguous_research_for_a_client_that_can_elicit(
    open_session: Any, call_extended: Any
) -> None:
    """The real server may ask the client (elicitation) for clarification; this client never
    answers. Observed behavior is asserted loosely: the task exists and keeps a valid status."""
    async with open_session({"elicitation": {}}) as client:
        created: Any = await call_extended(
            client,
            "simulate-research-query",
            {"topic": "jaguar", "ambiguous": True},
            task={},
            compare=task_times("task."),
        )
        task_id = created["task"]["taskId"]
        await asyncio.sleep(5)
        status: Any = await client.request("tasks/get", {"taskId": task_id}, compare=task_times(""))
    assert status["status"] in {"working", "input_required", "completed"}


async def test_unknown_tasks(mcp: McpClient) -> None:
    for method in ("tasks/get", "tasks/result", "tasks/cancel"):
        answer: Any = await mcp.request(method, {"taskId": "0123456789abcdef0123456789abcdef"})
        assert answer["code"] == -32602
    assert await mcp.request("tasks/list", {}) == {"tasks": [], "_meta": {}}


# -- tools that call back into the client --------------------------------------------------------


@pytest.mark.parametrize(
    ("capability", "tool", "arguments"),
    [
        ("sampling", "trigger-sampling-request", {"prompt": "Name a color", "maxTokens": 5}),
        ("elicitation", "trigger-elicitation-request", {}),
    ],
)
async def test_client_that_never_answers_the_server(
    open_session: Any, capability: str, tool: str, arguments: dict[str, Any]
) -> None:
    """The client declares the capability, then ignores the server's request to it."""
    async with open_session({capability: {}}, timeout_s=90.0) as client:
        result: Any = await client.call_tool(tool, arguments)
    assert result["isError"] is True
    assert result["content"][0]["text"] == "MCP error -32001: Request timed out"


async def test_roots_before_the_client_sent_any(open_session: Any) -> None:
    async with open_session({"roots": {"listChanged": True}}) as client:
        result: Any = await client.call_tool("get-roots-list", {})
    assert "isError" not in result
    assert result["content"][0]["text"].startswith(
        "The client supports roots but no roots are currently configured."
    )
