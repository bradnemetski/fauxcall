"""What a session keeps: gzip resources, simulated logging and updates, research tasks."""

from __future__ import annotations

import base64
import gzip
from typing import TYPE_CHECKING

import pytest
from fauxcall.model.fields import VolatilePolicy
from fauxcall.testing import CompareOptions
from scenario_support import TEXTS, dig, task_times, text, tool_call

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonValue

SESSION = "demo://resource/session/"


def _unzipped(blob: JsonValue) -> bytes:
    return gzip.decompress(base64.b64decode(str(blob)))


async def test_gzip_a_data_url_into_a_resource(mcp: McpClient) -> None:
    data = "data:text/csv;base64," + base64.b64encode(b"city,temp\nOslo,4\nLima,19\n").decode()
    made = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "weather.csv.gz", "data": data, "outputType": "resource"}
    )
    resource = dig(made, "content", 0, "resource")
    assert dig(resource, "uri") == SESSION + "weather.csv.gz"
    assert _unzipped(dig(resource, "blob")) == b"city,temp\nOslo,4\nLima,19\n"
    read = await mcp.request("resources/read", {"uri": SESSION + "weather.csv.gz"})
    assert dig(read, "contents", 0, "blob") == dig(resource, "blob")


async def test_gzip_links_are_listed_and_replaced(mcp: McpClient) -> None:
    first = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "notes.gz", "data": "data:,first%20draft"}
    )
    assert dig(first, "content", 0) == {
        "name": "notes.gz",
        "uri": SESSION + "notes.gz",
        "mimeType": "application/gzip",
        "type": "resource_link",
    }
    await mcp.call_tool("gzip-file-as-resource", {"name": "other.gz", "data": "data:,x"})
    await mcp.call_tool(
        "gzip-file-as-resource", {"name": "notes.gz", "data": "data:,second%20draft"}
    )
    read = await mcp.request("resources/read", {"uri": SESSION + "notes.gz"})
    assert _unzipped(dig(read, "contents", 0, "blob")) == b"second draft"
    listed = await mcp.request("resources/list", {})
    names = [dig(r, "name") for r in dig(listed, "resources")]  # type: ignore[union-attr]
    assert names[-2:] == ["other.gz", "notes.gz"]  # a replaced resource moves to the end
    missing = await mcp.request("resources/read", {"uri": SESSION + "drafts.gz"})
    assert missing == {
        "code": -32602,
        "message": f"MCP error -32602: Resource {SESSION}drafts.gz not found",
    }


async def test_gzip_of_the_default_remote_readme(mcp: McpClient) -> None:
    made = await mcp.call_tool("gzip-file-as-resource", {})
    assert dig(made, "content", 0, "uri") == SESSION + "README.md.gz"
    read = await mcp.request(
        "resources/read",
        {"uri": SESSION + "README.md.gz"},
        compare=CompareOptions(policies=[VolatilePolicy(path="$.contents[*].blob", scope="body")]),
    )
    assert _unzipped(dig(read, "contents", 0, "blob")).startswith(
        b"# Model Context Protocol servers"
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"data": "ftp://example.org/a.txt"},
            "Error processing file ftp://example.org/a.txt: Unsupported URL protocol for ftp://example.org/a.txt. Only http, https, and data URLs are supported.",
        ),
        (
            {"data": "file:///etc/passwd"},
            "Error processing file file:///etc/passwd: Unsupported URL protocol for file:///etc/passwd. Only http, https, and data URLs are supported.",
        ),
        (
            {"data": "example.org/a.txt"},
            "MCP error -32602: Input validation error: Invalid arguments for tool gzip-file-as-resource: Invalid URL at data",
        ),
        (
            {"data": "data:,x", "outputType": "inline"},
            'MCP error -32602: Input validation error: Invalid arguments for tool gzip-file-as-resource: Invalid option: expected one of "resourceLink"|"resource" at outputType',
        ),
        ({"data": "https://nonexistent.fauxcall-probe.invalid/x"}, "fetch failed"),
        ({"data": "data:;base64,%%%"}, "fetch failed"),
    ],
)
async def test_gzip_refusals(mcp: McpClient, arguments: dict[str, object], message: str) -> None:
    result = await mcp.call_tool("gzip-file-as-resource", arguments)  # type: ignore[arg-type]
    assert dig(result, "isError") is True
    assert text(result) == message


async def test_simulated_logging_toggles(mcp: McpClient) -> None:
    on = await mcp.call_tool("toggle-simulated-logging", {}, compare=TEXTS)
    assert text(on).startswith("Started simulated, random-leveled logging for session ")
    off = await mcp.call_tool("toggle-simulated-logging", {}, compare=TEXTS)
    assert text(off).startswith("Stopped simulated logging for session ")
    again = await mcp.call_tool("toggle-simulated-logging", {"verbose": True}, compare=TEXTS)
    assert text(again).startswith("Started")
    await mcp.call_tool("toggle-simulated-logging", {}, compare=TEXTS)


async def test_subscriber_updates_toggle(mcp: McpClient) -> None:
    await mcp.request("resources/subscribe", {"uri": "demo://resource/dynamic/text/5"})
    on = await mcp.call_tool("toggle-subscriber-updates", {}, compare=TEXTS)
    assert text(on).endswith(
        "at a 5 second pace. Client will receive updates for any resources the it is subscribed to."
    )
    off = await mcp.call_tool("toggle-subscriber-updates", {}, compare=TEXTS)
    assert text(off).startswith("Stopped simulated resource updates for session ")


async def test_research_task_lifecycle(mcp: McpClient) -> None:
    created = await tool_call(
        mcp,
        {
            "name": "simulate-research-query",
            "arguments": {"topic": "tidal energy"},
            "task": {"ttl": 30000},
        },
        compare=task_times("$.task"),
    )
    task = dig(created, "task")
    assert dig(task, "status") == "working"
    assert dig(task, "ttl") == 300000  # the server's, whatever the client asks
    assert dig(task, "statusMessage") == "Gathering sources..."
    task_id = dig(task, "taskId")
    polled = await mcp.request("tasks/get", {"taskId": task_id}, compare=task_times())
    assert dig(polled, "status") == "working"
    result = await mcp.request("tasks/result", {"taskId": task_id})
    report = str(dig(result, "content", 0, "text"))
    assert report.startswith("# Research Report: tidal energy\n")
    assert dig(result, "_meta", "io.modelcontextprotocol/related-task", "taskId") == task_id
    done = await mcp.request("tasks/get", {"taskId": task_id}, compare=task_times())
    assert (dig(done, "status"), dig(done, "statusMessage")) == (
        "completed",
        "Generating report...",
    )
    listed = await mcp.request("tasks/list", {}, compare=task_times("$.tasks[*]"))
    assert [dig(t, "taskId") for t in dig(listed, "tasks")] == [task_id]  # type: ignore[union-attr]
    late = await mcp.request("tasks/cancel", {"taskId": task_id})
    assert late == {
        "code": -32602,
        "message": "MCP error -32602: Cannot cancel task in terminal status: completed",
    }
    again = await mcp.request("tasks/result", {"taskId": task_id})
    assert dig(again, "content", 0, "text") == report


async def test_research_task_cancelled(mcp: McpClient) -> None:
    created = await tool_call(
        mcp,
        {"name": "simulate-research-query", "arguments": {"topic": "glaciers"}, "task": {}},
        compare=task_times("$.task"),
    )
    task_id = dig(created, "task", "taskId")
    cancelled = await mcp.request("tasks/cancel", {"taskId": task_id}, compare=task_times())
    assert isinstance(cancelled, dict)
    assert list(cancelled) == [
        "_meta",
        "taskId",
        "status",
        "ttl",
        "createdAt",
        "lastUpdatedAt",
        "pollInterval",
        "statusMessage",
    ]
    assert dig(cancelled, "statusMessage") == "Client cancelled task execution."
    polled = await mcp.request("tasks/get", {"taskId": task_id}, compare=task_times())
    assert dig(polled, "status") == "cancelled"
    result = await mcp.request("tasks/result", {"taskId": task_id})
    assert result == {"code": -32603, "message": f"Task {task_id} has no result stored"}
    twice = await mcp.request("tasks/cancel", {"taskId": task_id})
    assert (
        dig(twice, "message")
        == "MCP error -32602: Cannot cancel task in terminal status: cancelled"
    )


async def test_research_needs_a_task_and_a_topic(mcp: McpClient) -> None:
    plain = await mcp.call_tool("simulate-research-query", {"topic": "rivers"})
    assert (
        text(plain)
        == "MCP error -32601: Tool simulate-research-query requires task augmentation (taskSupport: 'required')"
    )
    no_topic = await tool_call(
        mcp,
        {"name": "simulate-research-query", "arguments": {"ambiguous": "yes"}, "task": {}},
    )
    assert dig(no_topic, "code") == -32602
    assert str(dig(no_topic, "message")).startswith(
        "MCP error -32602: Invalid task creation result: "
    )


async def test_ambiguous_research_without_elicitation_is_not_asked(mcp: McpClient) -> None:
    created = await tool_call(
        mcp,
        {
            "name": "simulate-research-query",
            "arguments": {"topic": "Python decorators", "ambiguous": True},
            "task": {},
        },
        compare=task_times("$.task"),
    )
    result = await mcp.request("tasks/result", {"taskId": dig(created, "task", "taskId")})
    report = str(dig(result, "content", 0, "text"))
    assert report.startswith("# Research Report: Python decorators\n")
    assert "Clarification" not in report
