"""State a session builds up: gzipped files, toggles, research tasks."""

from __future__ import annotations

import asyncio
import base64
import gzip
from typing import TYPE_CHECKING, Any

from scenario_support import ISO_MS, STAMPED_TASK, TASK_ID, call_with, texts

if TYPE_CHECKING:
    from fauxcall.testing import McpClient


def _data_uri(text: str) -> str:
    return "data:text/plain;base64," + base64.b64encode(text.encode()).decode()


async def test_gzip_cycle(mcp: McpClient) -> None:
    """Create, read back, replace, read again; the file is listed with the documents."""
    created: Any = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "report.csv.gz", "data": _data_uri("a,b\n1,2\n")}
    )
    link = created["content"][0]
    assert link == {
        "name": "report.csv.gz",
        "uri": "demo://resource/session/report.csv.gz",
        "mimeType": "application/gzip",
        "type": "resource_link",
    }
    read: Any = await mcp.request("resources/read", {"uri": link["uri"]}, label="read gzip")
    assert gzip.decompress(base64.b64decode(read["contents"][0]["blob"])) == b"a,b\n1,2\n"
    listed: Any = await mcp.request("resources/list", {}, label="resources/list after gzip")
    assert listed["resources"][-1]["uri"] == link["uri"]
    await mcp.call_tool(
        "gzip-file-as-resource",
        {"name": "report.csv.gz", "data": "data:text/csv,x%2Cy%0A3%2C4", "outputType": "resource"},
    )
    again: Any = await mcp.request("resources/read", {"uri": link["uri"]}, label="read replaced")
    assert gzip.decompress(base64.b64decode(again["contents"][0]["blob"])) == b"x,y\n3,4"


async def test_gzip_refusals(mcp: McpClient) -> None:
    await mcp.call_tool("gzip-file-as-resource", {"name": "x.gz", "data": "gopher://example.com/x"})
    await mcp.call_tool(
        "gzip-file-as-resource", {"name": "x.gz", "data": "http://localhost:1/none"}
    )
    await mcp.call_tool("gzip-file-as-resource", {"name": "x.gz", "data": "::"})
    await mcp.call_tool("gzip-file-as-resource", {"outputType": "link"})


async def test_session_files_belong_to_their_session(mcp: McpClient) -> None:
    listed: Any = await mcp.request("resources/list", {}, label="resources/list fresh session")
    assert all(not r["uri"].startswith("demo://resource/session/") for r in listed["resources"])
    await mcp.request(
        "resources/read", {"uri": "demo://resource/session/report.csv.gz"}, label="other session"
    )


async def test_toggles_answer_on_and_off(mcp: McpClient) -> None:
    on: Any = await mcp.call_tool("toggle-simulated-logging", {})
    off: Any = await mcp.call_tool("toggle-simulated-logging", {"ignored": True})
    assert texts(on)[0].startswith("Started simulated, random-leveled logging for session ")
    assert texts(off)[0].startswith("Stopped simulated logging for session ")
    sub_on: Any = await mcp.call_tool("toggle-subscriber-updates", {})
    sub_off: Any = await mcp.call_tool("toggle-subscriber-updates", {})
    assert texts(sub_on)[0].startswith("Started simulated resource updated notifications")
    assert texts(sub_off)[0].startswith("Stopped simulated resource updates for session ")
    session = texts(on)[0].split("for session ", 1)[1].split(" ", 1)[0]
    assert texts(off)[0].endswith(session)


async def test_research_task_lifecycle(mcp: McpClient) -> None:
    created: Any = await call_with(
        mcp,
        "simulate-research-query",
        {"topic": "coral reefs"},
        {"task": {"ttl": 30000}},
        compare=STAMPED_TASK,
    )
    task = created["task"]
    assert TASK_ID.match(task["taskId"])
    assert ISO_MS.match(task["createdAt"])
    assert (task["status"], task["ttl"], task["pollInterval"]) == ("working", 300000, 1000)
    statuses = []
    for _ in range(8):
        got: Any = await mcp.request(
            "tasks/get", {"taskId": task["taskId"]}, label="tasks/get", compare=STAMPED_TASK
        )
        statuses.append(got["status"])
        if got["status"] == "completed":
            break
        await asyncio.sleep(1)
    assert statuses[-1] == "completed"
    result: Any = await mcp.request(
        "tasks/result", {"taskId": task["taskId"]}, label="tasks/result", compare=STAMPED_TASK
    )
    assert texts(result)[0].startswith("# Research Report: coral reefs\n")
    listed: Any = await mcp.request("tasks/list", {}, label="tasks/list", compare=STAMPED_TASK)
    assert [t["taskId"] for t in listed["tasks"]] == [task["taskId"]]


async def test_research_result_waits_for_completion(mcp: McpClient) -> None:
    created: Any = await call_with(
        mcp,
        "simulate-research-query",
        {"topic": "volcanoes", "ambiguous": True},
        {"task": {}},
        compare=STAMPED_TASK,
    )
    result: Any = await mcp.request(
        "tasks/result",
        {"taskId": created["task"]["taskId"]},
        label="tasks/result",
        compare=STAMPED_TASK,
    )
    # no elicitation capability: the ambiguity is ignored
    assert texts(result)[0].startswith("# Research Report: volcanoes\n")


async def test_research_with_clarification(mcp: McpClient) -> None:
    await mcp.open(
        capabilities={"elicitation": {}},
        replies={
            "elicitation/create": {"action": "accept", "content": {"interpretation": "comedy"}}
        },
    )
    created: Any = await call_with(
        mcp,
        "simulate-research-query",
        {"topic": "Monty Python", "ambiguous": True},
        {"task": {}},
        compare=STAMPED_TASK,
    )
    result: Any = await mcp.request(
        "tasks/result",
        {"taskId": created["task"]["taskId"]},
        label="tasks/result",
        compare=STAMPED_TASK,
    )
    assert texts(result)[0].startswith("# Research Report: Monty Python (comedy)\n")


async def test_research_cancel_and_errors(mcp: McpClient) -> None:
    created: Any = await call_with(
        mcp,
        "simulate-research-query",
        {"topic": "short-lived"},
        {"task": {"ttl": 1000}},
        compare=STAMPED_TASK,
    )
    task_id = created["task"]["taskId"]
    cancelled: Any = await mcp.request(
        "tasks/cancel", {"taskId": task_id}, label="tasks/cancel", compare=STAMPED_TASK
    )
    assert (cancelled["status"], cancelled["statusMessage"]) == (
        "cancelled",
        "Client cancelled task execution.",
    )
    await mcp.request(
        "tasks/cancel", {"taskId": task_id}, label="cancel again", compare=STAMPED_TASK
    )
    await mcp.request(
        "tasks/result", {"taskId": task_id}, label="result of cancelled", compare=STAMPED_TASK
    )
    await mcp.request("tasks/get", {"taskId": "0" * 32}, label="tasks/get unknown")
    await mcp.request("tasks/result", {"taskId": "nope"}, label="tasks/result unknown")
    await call_with(mcp, "simulate-research-query", {"topic": ["x"]}, {"task": {}})
