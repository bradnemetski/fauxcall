"""Tools that change session state: gzip resources, research tasks, simulations.

These tools aren't annotated read-only, so on the real server each call spends the user's
approval (``--allow-real-writes``). Everything they change lives in the MCP session and ends
with it. Writes per run are kept few: the budget covers recording and every parity run.
"""

from __future__ import annotations

import base64
import gzip
import json
from typing import TYPE_CHECKING, Any

from everything_support import (
    call_with,
    now_fields,
    read_stream_messages,
    text_of,
    wait_for_task,
)

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

LEVELS = {"debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"}
TASK_TIMES = now_fields("$.task.createdAt", "$.task.lastUpdatedAt")


def _data_url(text: str) -> str:
    return "data:text/plain;base64," + base64.b64encode(text.encode()).decode()


async def test_gzip_link_then_read_and_list(mcp: McpClient) -> None:
    content = "parity: gzip me, then read me back\n"
    made: Any = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "parity-notes.txt.gz", "data": _data_url(content)}
    )
    link = made["content"][0]
    assert link["type"] == "resource_link"
    assert link["uri"] == "demo://resource/session/parity-notes.txt.gz"
    read: Any = await mcp.request("resources/read", {"uri": link["uri"]})
    blob = read["contents"][0]["blob"]
    assert gzip.decompress(base64.b64decode(blob)).decode() == content
    listed: Any = await mcp.request("resources/list", {}, label="resources/list")
    assert listed["resources"][-1]["uri"] == link["uri"]
    inline: Any = await mcp.call_tool(
        "gzip-file-as-resource",
        {
            "name": "parity-notes.txt.gz",
            "data": "data:,replaced%20content",
            "outputType": "resource",
        },
    )
    resource = inline["content"][0]["resource"]
    assert gzip.decompress(base64.b64decode(resource["blob"])) == b"replaced content"
    again: Any = await mcp.request("resources/read", {"uri": link["uri"]})
    assert again["contents"][0]["blob"] == resource["blob"]
    listed = await mcp.request("resources/list", {}, label="resources/list")
    uris = [r["uri"] for r in listed["resources"]]
    assert uris.count(link["uri"]) == 1


async def test_gzip_refusals(mcp: McpClient) -> None:
    unsupported: Any = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "x.gz", "data": "mailto:someone@example.com"}
    )
    assert text_of(unsupported).startswith("Error processing file mailto:someone@example.com: ")
    broken: Any = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "x.gz", "data": "data:;base64,%%%"}
    )
    assert text_of(broken) == "fetch failed"
    invalid: Any = await mcp.call_tool(
        "gzip-file-as-resource", {"name": "x.gz", "data": "no-scheme"}
    )
    assert text_of(invalid).endswith("Invalid URL at data")


async def test_research_task_runs_to_completion(mcp: McpClient) -> None:
    created: Any = await call_with(
        mcp,
        "simulate-research-query",
        {"arguments": {"topic": "parity of fakes"}, "task": {"ttl": 120000}},
        compare=TASK_TIMES,
    )
    task = created["task"]
    assert (task["status"], task["ttl"], task["pollInterval"]) == ("working", 300000, 1000)
    assert task["statusMessage"] == "Gathering sources..."
    finished: Any = await wait_for_task(mcp, task["taskId"])
    assert finished["status"] == "completed"
    assert finished["statusMessage"] == "Generating report..."
    result: Any = await mcp.request(
        "tasks/result", {"taskId": task["taskId"]}, label="tasks/result"
    )
    assert text_of(result).startswith("# Research Report: parity of fakes\n")
    assert result["_meta"]["io.modelcontextprotocol/related-task"]["taskId"] == task["taskId"]
    listed: Any = await mcp.request(
        "tasks/list",
        {},
        label="tasks/list",
        compare=now_fields("$.tasks[0].createdAt", "$.tasks[0].lastUpdatedAt"),
    )
    assert [t["taskId"] for t in listed["tasks"]] == [task["taskId"]]
    refused: Any = await mcp.request(
        "tasks/cancel", {"taskId": task["taskId"]}, label="tasks/cancel"
    )
    assert (
        refused["message"] == "MCP error -32602: Cannot cancel task in terminal status: completed"
    )


async def test_research_task_cancelled(mcp: McpClient) -> None:
    created: Any = await call_with(
        mcp,
        "simulate-research-query",
        {"arguments": {"topic": "cancel me", "ambiguous": True}, "task": {}},
        compare=TASK_TIMES,
    )
    task_id = created["task"]["taskId"]
    cancelled: Any = await mcp.request(
        "tasks/cancel",
        {"taskId": task_id},
        label="tasks/cancel",
        compare=now_fields("$.createdAt", "$.lastUpdatedAt"),
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["statusMessage"] == "Client cancelled task execution."
    got: Any = await mcp.request(
        "tasks/get",
        {"taskId": task_id},
        label="tasks/get",
        compare=now_fields("$.createdAt", "$.lastUpdatedAt"),
    )
    assert got["status"] == "cancelled"
    no_result: Any = await mcp.request("tasks/result", {"taskId": task_id}, label="tasks/result")
    assert no_result["code"] == -32603
    assert no_result["message"].endswith("has no result stored")


async def test_task_refusals(mcp: McpClient) -> None:
    for method in ("tasks/get", "tasks/result", "tasks/cancel"):
        error: Any = await mcp.request(method, {"taskId": "0000-not-a-task"}, label=method)
        assert error["code"] == -32602
        assert "not found" in error["message"]
    missing: Any = await mcp.request("tasks/get", {"id": "x"}, label="tasks/get")
    assert missing["code"] == -32603
    listed: Any = await mcp.request("tasks/list", {}, label="tasks/list")
    assert listed["tasks"] == []


async def test_research_needs_a_task(mcp: McpClient) -> None:
    plain: Any = await mcp.call_tool("simulate-research-query", {"topic": "no task"})
    assert plain["isError"] is True
    assert "requires task augmentation" in text_of(plain)
    invalid: Any = await call_with(
        mcp, "simulate-research-query", {"arguments": {"topic": 3}, "task": {}}
    )
    assert invalid["code"] == -32602
    assert invalid["message"].startswith("MCP error -32602: Invalid task creation result: ")


async def test_simulated_logging_toggles(mcp: McpClient) -> None:
    started: Any = await mcp.call_tool("toggle-simulated-logging", {})
    assert text_of(started).startswith("Started simulated, random-leveled logging for session ")
    stopped: Any = await mcp.call_tool("toggle-simulated-logging", {})
    assert text_of(stopped).startswith("Stopped simulated logging for session ")


async def test_subscriber_updates_toggle(mcp: McpClient) -> None:
    await mcp.request("resources/subscribe", {"uri": "demo://resource/dynamic/text/3"})
    started: Any = await mcp.call_tool("toggle-subscriber-updates", {})
    assert text_of(started).startswith("Started simulated resource updated notifications")
    stopped: Any = await mcp.call_tool("toggle-subscriber-updates", {})
    assert text_of(stopped).startswith("Stopped simulated resource updates for session ")


async def test_pushed_log_and_update_messages(mcp: McpClient) -> None:
    """What the GET stream carries: subscribe logs, resource updates, simulated log messages."""
    await mcp.request("logging/setLevel", {"level": "debug"})
    uri = "demo://resource/dynamic/text/8"
    session = mcp.wire.session_id

    async def subscribe() -> Any:
        return await mcp.request("resources/subscribe", {"uri": uri})

    messages = await read_stream_messages(mcp, subscribe, wanted=1)
    if messages is not None:
        assert messages[0]["method"] == "notifications/message"
        assert messages[0]["params"] == {
            "level": "info",
            "data": f"Received Subscribe Resource request for URI: {uri} from session {session}",
        }

    async def updates_on() -> Any:
        return await mcp.call_tool("toggle-subscriber-updates", {})

    messages = await read_stream_messages(mcp, updates_on, wanted=1)
    if messages is not None:
        assert messages[0]["method"] == "notifications/resources/updated"
        assert messages[0]["params"] == {"uri": uri}
    await mcp.call_tool("toggle-subscriber-updates", {})

    async def logging_on() -> Any:
        return await mcp.call_tool("toggle-simulated-logging", {})

    messages = await read_stream_messages(mcp, logging_on, wanted=1)
    if messages is not None:
        log: Any = messages[0]["params"]
        assert log["level"] in LEVELS
        assert log["data"].endswith(f" - SessionId {session}")
    await mcp.call_tool("toggle-simulated-logging", {})


async def test_capability_tools_are_unknown_to_this_client(mcp: McpClient) -> None:
    """The scenario client declares no sampling, elicitation or roots: the server hides them."""
    for name in (
        "trigger-sampling-request",
        "trigger-elicitation-request",
        "trigger-url-elicitation",
        "trigger-sampling-request-async",
        "trigger-elicitation-request-async",
    ):
        result: Any = await mcp.call_tool(name, {})
        assert result["isError"] is True
        assert text_of(result) == f"MCP error -32602: Tool {name} not found"


def test_data_url_helper() -> None:
    assert json.dumps(_data_url("a")) == '"data:text/plain;base64,YQ=="'


async def test_gzip_default_source(mcp: McpClient) -> None:
    """No arguments: the server compresses its own README (the fake replays the recorded one)."""
    made: Any = await mcp.call_tool("gzip-file-as-resource", {})
    assert made["content"][0] == {
        "name": "README.md.gz",
        "uri": "demo://resource/session/README.md.gz",
        "mimeType": "application/gzip",
        "type": "resource_link",
    }
    listed: Any = await mcp.request("resources/list", {}, label="resources/list")
    assert listed["resources"][-1]["name"] == "README.md.gz"
