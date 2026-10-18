"""Tools that change the session: gzip resources, the toggles, and research tasks.

These are tool calls the server doesn't mark read-only: on the real server each spends one call
of the approved budget (``--allow-real-writes``), so the scenarios stay lean.
"""

from __future__ import annotations

import base64
import gzip
from typing import TYPE_CHECKING, Any, Final

from policies import CREATED_TASK, SESSION_TEXT, TASK, TASKS

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

SESSION: Final = "demo://resource/session/"


def text(result: Any, index: int = 0) -> str:
    return str(result["content"][index]["text"])


async def session_uris(mcp: McpClient) -> list[str]:
    listing: Any = await mcp.request("resources/list", {})
    return [r["uri"] for r in listing["resources"] if r["uri"].startswith(SESSION)]


async def test_gzip_link_list_read_replace_cycle(mcp: McpClient) -> None:
    data = "data:text/plain;base64," + base64.b64encode(b"parity payload").decode()
    link: Any = await mcp.call_tool("gzip-file-as-resource", {"name": "cycle.txt.gz", "data": data})
    assert link["content"][0] == {
        "name": "cycle.txt.gz",
        "uri": f"{SESSION}cycle.txt.gz",
        "mimeType": "application/gzip",
        "type": "resource_link",
    }
    assert await session_uris(mcp) == [f"{SESSION}cycle.txt.gz"]
    first: Any = await mcp.request("resources/read", {"uri": f"{SESSION}cycle.txt.gz"})
    assert gzip.decompress(base64.b64decode(first["contents"][0]["blob"])) == b"parity payload"
    await mcp.call_tool(
        "gzip-file-as-resource",
        {"name": "other.gz", "data": "data:,second", "outputType": "resourceLink"},
    )
    await mcp.call_tool("gzip-file-as-resource", {"name": "cycle.txt.gz", "data": "data:,replaced"})
    assert await session_uris(mcp) == [f"{SESSION}other.gz", f"{SESSION}cycle.txt.gz"]
    second: Any = await mcp.request("resources/read", {"uri": f"{SESSION}cycle.txt.gz"})
    assert gzip.decompress(base64.b64decode(second["contents"][0]["blob"])) == b"replaced"


async def test_gzip_embedded_resource_and_failures(mcp: McpClient) -> None:
    embedded: Any = await mcp.call_tool(
        "gzip-file-as-resource",
        {
            "name": "inline.json.gz",
            "data": "data:application/json,%7B%22ok%22%3Atrue%7D",
            "outputType": "resource",
        },
    )
    resource = embedded["content"][0]["resource"]
    assert gzip.decompress(base64.b64decode(resource["blob"])) == b'{"ok":true}'
    blocked: Any = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "x", "data": "http://127.0.0.1:9/nothing-here"}
    )
    ftp: Any = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "x", "data": "sftp://host/file"}
    )
    bad: Any = await mcp.call_tool("gzip-file-as-resource", {"data": "::"})
    assert text(blocked) == "fetch failed"
    assert "Unsupported URL protocol for sftp://host/file." in text(ftp)
    assert bad["isError"] is True
    assert await session_uris(mcp) == [f"{SESSION}inline.json.gz"]


async def test_gzip_names_with_spaces_list_but_do_not_read(mcp: McpClient) -> None:
    await mcp.call_tool("gzip-file-as-resource", {"name": "two words.gz", "data": "data:,x"})
    read: Any = await mcp.request("resources/read", {"uri": f"{SESSION}two words.gz"})
    encoded: Any = await mcp.request("resources/read", {"uri": f"{SESSION}two%20words.gz"})
    assert await session_uris(mcp) == [f"{SESSION}two words.gz"]
    assert (
        read
        == encoded
        == {
            "code": -32602,
            "message": f"MCP error -32602: Resource {SESSION}two%20words.gz not found",
        }
    )


async def test_toggles_start_and_stop_per_session(mcp: McpClient) -> None:
    on: Any = await mcp.call_tool("toggle-simulated-logging", {}, compare=SESSION_TEXT)
    off: Any = await mcp.call_tool("toggle-simulated-logging", {}, compare=SESSION_TEXT)
    updates_on: Any = await mcp.call_tool("toggle-subscriber-updates", {}, compare=SESSION_TEXT)
    updates_off: Any = await mcp.call_tool("toggle-subscriber-updates", {}, compare=SESSION_TEXT)
    assert text(on).startswith("Started simulated, random-leveled logging for session ")
    assert text(off).startswith("Stopped simulated logging for session ")
    assert text(updates_on).startswith("Started simulated resource updated notifications")
    assert text(updates_off).startswith("Stopped simulated resource updates for session ")


async def test_research_is_refused_without_a_task(mcp: McpClient) -> None:
    refused: Any = await mcp.call_tool("simulate-research-query", {"topic": "anything"})
    assert refused == {
        "content": [
            {
                "type": "text",
                "text": "MCP error -32601: Tool simulate-research-query requires task "
                "augmentation (taskSupport: 'required')",
            }
        ],
        "isError": True,
    }


async def test_research_task_lifecycle(mcp: McpClient) -> None:
    created: Any = await mcp.request(
        "tools/call",
        {
            "name": "simulate-research-query",
            "arguments": {"topic": "tide pools"},
            "task": {"ttl": 5000},
        },
        label="tools/call:simulate-research-query",
        compare=CREATED_TASK,
    )
    task = created["task"]
    assert (task["status"], task["ttl"], task["statusMessage"]) == (
        "working",
        300000,
        "Gathering sources...",
    )
    task_id = task["taskId"]
    early: Any = await mcp.request("tasks/get", {"taskId": task_id}, compare=TASK)
    assert early["status"] == "working"
    result: Any = await mcp.request("tasks/result", {"taskId": task_id})
    assert result["content"][0]["text"].startswith("# Research Report: tide pools\n")
    assert result["_meta"] == {"io.modelcontextprotocol/related-task": {"taskId": task_id}}
    done: Any = await mcp.request("tasks/get", {"taskId": task_id}, compare=TASK)
    assert (done["status"], done["statusMessage"]) == ("completed", "Generating report...")
    again: Any = await mcp.request("tasks/result", {"taskId": task_id})
    assert again == result
    listed: Any = await mcp.request("tasks/list", {}, compare=TASKS)
    assert [t["taskId"] for t in listed["tasks"]] == [task_id]
    finished: Any = await mcp.request("tasks/cancel", {"taskId": task_id})
    assert (
        finished["message"] == "MCP error -32602: Cannot cancel task in terminal status: completed"
    )


async def test_cancelling_a_research_task(mcp: McpClient) -> None:
    created: Any = await mcp.request(
        "tools/call",
        {
            "name": "simulate-research-query",
            "arguments": {"topic": "kelp", "ambiguous": True},
            "task": {},
        },
        label="tools/call:simulate-research-query",
        compare=CREATED_TASK,
    )
    task_id = created["task"]["taskId"]
    cancelled: Any = await mcp.request("tasks/cancel", {"taskId": task_id}, compare=TASK)
    assert next(iter(cancelled)) == "_meta"
    assert (cancelled["status"], cancelled["statusMessage"]) == (
        "cancelled",
        "Client cancelled task execution.",
    )
    after: Any = await mcp.request("tasks/get", {"taskId": task_id}, compare=TASK)
    assert after["status"] == "cancelled"
    no_result: Any = await mcp.request("tasks/result", {"taskId": task_id})
    assert no_result == {"code": -32603, "message": f"Task {task_id} has no result stored"}
    twice: Any = await mcp.request("tasks/cancel", {"taskId": task_id})
    assert twice["code"] == -32602


async def test_research_refusals_with_a_task(mcp: McpClient) -> None:
    missing: Any = await mcp.request(
        "tools/call",
        {"name": "simulate-research-query", "arguments": {"ambiguous": True}, "task": {"ttl": 1}},
        label="tools/call:simulate-research-query",
    )
    assert missing["code"] == -32602
    assert missing["message"].startswith("MCP error -32602: Invalid task creation result: [")


async def test_tasks_methods_on_unknown_tasks(mcp: McpClient) -> None:
    get: Any = await mcp.request("tasks/get", {"taskId": "0" * 32})
    result: Any = await mcp.request("tasks/result", {"taskId": "deadbeef"})
    cancel: Any = await mcp.request("tasks/cancel", {"taskId": "unknown-task"})
    listed: Any = await mcp.request("tasks/list", {})
    nameless: Any = await mcp.request("tasks/result", {})
    assert get == {
        "code": -32602,
        "message": "MCP error -32602: Failed to retrieve task: Task not found",
    }
    assert result == {"code": -32602, "message": "MCP error -32602: Task not found: deadbeef"}
    assert cancel == {"code": -32602, "message": "MCP error -32602: Task not found: unknown-task"}
    assert listed == {"tasks": [], "_meta": {}}
    assert nameless["code"] == -32603


async def test_a_read_only_tool_called_as_a_task(mcp: McpClient) -> None:
    refused: Any = await mcp.request(
        "tools/call",
        {"name": "get-sum", "arguments": {"a": 1, "b": 2}, "task": {"ttl": 1000}},
        label="tools/call:get-sum",
    )
    assert refused["code"] == -32602
    assert "Invalid task creation result" in refused["message"]


async def test_ambiguous_research_asks_through_tasks_result(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {}},
        replies={
            "elicitation/create": {"action": "accept", "content": {"interpretation": "comedy"}}
        },
    )
    created: Any = await mcp.request(
        "tools/call",
        {
            "name": "simulate-research-query",
            "arguments": {"topic": "Python sketches", "ambiguous": True},
            "task": {"ttl": 60000},
        },
        label="tools/call:simulate-research-query",
        compare=CREATED_TASK,
    )
    task_id = created["task"]["taskId"]
    result: Any = await mcp.request("tasks/result", {"taskId": task_id})
    report = result["content"][0]["text"]
    assert report.startswith("# Research Report: Python sketches (comedy)\n")
    assert "`working` → `input_required` → `working` → `completed`" in report
    done: Any = await mcp.request("tasks/get", {"taskId": task_id}, compare=TASK)
    assert done["status"] == "completed"


async def test_ambiguous_research_without_elicitation_skips_the_question(mcp: McpClient) -> None:
    created: Any = await mcp.request(
        "tools/call",
        {
            "name": "simulate-research-query",
            "arguments": {"topic": "Mercury", "ambiguous": True},
            "task": {},
        },
        label="tools/call:simulate-research-query",
        compare=CREATED_TASK,
    )
    result: Any = await mcp.request("tasks/result", {"taskId": created["task"]["taskId"]})
    assert result["content"][0]["text"].startswith("# Research Report: Mercury\n")
