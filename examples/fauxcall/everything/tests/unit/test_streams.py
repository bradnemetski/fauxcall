"""What the fake sends on a client's GET stream, outside any call (recorded streams and source).

Simulated log messages and resource updates (every 5 s, shrunk here), resource list changes from
``gzip-file-as-resource`` (also to the session whose resource another one replaced), task status
notifications, the roots requests, and the tool list changes after ``notifications/initialized``.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Final

import pytest
from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests
from wire_helpers import client, heard, methods, rpc, tool

from fauxcall_everything import tasks, tools
from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fauxcall.types import JsonObject

SIMULATION_S: Final = 0.2
"""The 5-second simulation pace, shrunk."""
TICK: Final = 0.05
LIST_CHANGED: Final = {"method": "notifications/resources/list_changed", "jsonrpc": "2.0"}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(tools, "SIMULATION_SECONDS", SIMULATION_S)
    monkeypatch.setattr(tasks, "TICK", TICK)
    return build_app(settings_for_tests(Settings))


# -- resources -------------------------------------------------------------------------------------


async def test_gzip_tells_the_client_its_list_changed(app: FastAPI) -> None:
    data = "data:text/plain,hi"
    async with client(app) as wire:
        await tool(wire, "gzip-file-as-resource", {"name": "a.gz", "data": data})
        await tool(wire, "gzip-file-as-resource", {"name": "b.gz", "data": data})
        await tool(wire, "gzip-file-as-resource", {"name": "a.gz", "data": data})  # replaced
        messages = await heard(wire)
    assert messages == [LIST_CHANGED] * 4  # as recorded: a replacement removes, then adds


async def test_registering_a_name_another_session_used_takes_it_from_that_session(
    app: FastAPI,
) -> None:
    data = "data:text/plain,hi"
    async with client(app) as first, client(app, listen=False) as second:
        await tool(first, "gzip-file-as-resource", {"name": "shared.gz", "data": data})
        await tool(second, "gzip-file-as-resource", {"name": "shared.gz", "data": data})
        listed = await rpc(first, "resources/list", {})
        read = await rpc(first, "resources/read", {"uri": "demo://resource/session/shared.gz"})
        mine = await rpc(second, "resources/read", {"uri": "demo://resource/session/shared.gz"})
        messages = await heard(first)
    assert messages == [LIST_CHANGED, LIST_CHANGED]  # registered, then removed by the other
    assert isinstance(listed.result, dict)
    assert all("session" not in r["uri"] for r in listed.result["resources"])
    assert read.error == {
        "code": -32602,
        "message": "MCP error -32602: Resource demo://resource/session/shared.gz not found",
    }
    assert isinstance(mine.result, dict)


async def test_subscriptions_log_and_simulated_updates_follow_them(app: FastAPI) -> None:
    text_1, features = (
        "demo://resource/dynamic/text/1",
        "demo://resource/static/document/features.md",
    )
    async with client(app) as wire:
        await rpc(wire, "resources/subscribe", {"uri": text_1})
        await rpc(wire, "resources/subscribe", {"uri": features})
        started = await tool(wire, "toggle-subscriber-updates")
        await asyncio.sleep(SIMULATION_S * 1.5)
        await rpc(wire, "resources/unsubscribe", {"uri": text_1})
        await asyncio.sleep(SIMULATION_S)
        stopped = await tool(wire, "toggle-subscriber-updates")
        session = wire.session_id
        messages = await heard(wire, SIMULATION_S * 1.5)
    assert started["content"][0]["text"].startswith(
        f"Started simulated resource updated notifications for session {session}"
    )
    assert (
        stopped["content"][0]["text"] == f"Stopped simulated resource updates for session {session}"
    )
    logs = [m["params"] for m in messages if m["method"] == "notifications/message"]
    assert logs == [
        {
            "level": "info",
            "data": f"Received Subscribe Resource request for URI: {text_1} from session {session}",
        },
        {
            "level": "info",
            "data": f"Received Subscribe Resource request for URI: {features} "
            f"from session {session}",
        },
        {
            "level": "info",
            "data": f"Received Unsubscribe Resource request: {text_1} from session {session}",
        },
    ]
    updated = [
        m["params"]["uri"] for m in messages if m["method"] == "notifications/resources/updated"
    ]
    assert updated[:4] == [text_1, features, text_1, features]  # at once, then 5 s later
    assert updated[-1] == features  # after the unsubscription only the other one
    assert text_1 not in updated[updated.index(features, 3) + 1 :]


async def test_updates_without_subscriptions_send_nothing_until_one_is_made(app: FastAPI) -> None:
    async with client(app) as wire:
        await tool(wire, "toggle-subscriber-updates")
        await asyncio.sleep(SIMULATION_S * 1.2)
        await rpc(wire, "resources/subscribe", {"uri": "demo://resource/dynamic/blob/2"})
        await asyncio.sleep(SIMULATION_S * 1.2)
        await tool(wire, "toggle-subscriber-updates")
        messages = await heard(wire)
    assert methods(messages)[0] == "notifications/message"  # the subscription's log comes first
    assert {
        "method": "notifications/resources/updated",
        "params": {"uri": "demo://resource/dynamic/blob/2"},
        "jsonrpc": "2.0",
    } in messages


# -- logging ---------------------------------------------------------------------------------------


_LOG_DATA: Final = re.compile(
    r"^(Debug-level|Info-level|Notice-level|Warning-level|Error-level|Critical-level|"
    r"Emergency-level) message - SessionId [0-9a-f-]{36}$"
    r"|^Alert level-message - SessionId [0-9a-f-]{36}$"
)


async def test_simulated_logging_respects_the_chosen_level(app: FastAPI) -> None:
    async with client(app) as wire:
        await rpc(wire, "logging/setLevel", {"level": "emergency"})
        on = await tool(wire, "toggle-simulated-logging")
        await asyncio.sleep(SIMULATION_S * 30)
        off = await tool(wire, "toggle-simulated-logging")
        messages = await heard(wire, SIMULATION_S * 2)
    assert "Started simulated, random-leveled logging" in on["content"][0]["text"]
    assert off["content"][0]["text"].startswith("Stopped simulated logging for session ")
    assert messages, "an emergency message comes about one time in eight"
    assert all(m["params"]["level"] == "emergency" for m in messages)
    assert all(_LOG_DATA.match(m["params"]["data"]) for m in messages)


async def test_simulated_logging_sends_one_at_once_then_every_interval(app: FastAPI) -> None:
    async with client(app) as wire:
        await tool(wire, "toggle-simulated-logging")
        await asyncio.sleep(SIMULATION_S * 2.5)
        await tool(wire, "toggle-simulated-logging")
        messages = await heard(wire, SIMULATION_S * 2)
    assert len(messages) == 3
    assert methods(messages) == ["notifications/message"] * 3


# -- tasks -----------------------------------------------------------------------------------------


async def test_research_status_notifications_follow_the_stages(app: FastAPI) -> None:
    async with client(app) as wire:
        created = await rpc(
            wire,
            "tools/call",
            {"name": "simulate-research-query", "arguments": {"topic": "volcanoes"}, "task": {}},
        )
        assert isinstance(created.result, dict)
        task_id = created.result["task"]["taskId"]
        result = await rpc(wire, "tasks/result", {"taskId": task_id})
        messages = await heard(wire)
    statuses = [(m["params"]["status"], m["params"]["statusMessage"]) for m in messages]
    assert statuses == [
        ("working", "Gathering sources..."),
        ("working", "Analyzing content..."),
        ("working", "Synthesizing findings..."),
        ("working", "Generating report..."),
        ("completed", "Generating report..."),
    ]
    assert isinstance(result.result, dict)
    assert result.result["_meta"] == {"io.modelcontextprotocol/related-task": {"taskId": task_id}}


async def test_a_cancelled_research_says_nothing_more(app: FastAPI) -> None:
    async with client(app) as wire:
        created = await rpc(
            wire,
            "tools/call",
            {
                "name": "simulate-research-query",
                "arguments": {"topic": "glaciers"},
                "task": {"ttl": 1},
            },
        )
        assert isinstance(created.result, dict)
        task_id = created.result["task"]["taskId"]
        await rpc(wire, "tasks/cancel", {"taskId": task_id})
        await asyncio.sleep(TICK * 6)
        got = await rpc(wire, "tasks/get", {"taskId": task_id})
        messages = await heard(wire)
    assert [m["params"]["status"] for m in messages] == ["working"]
    assert isinstance(got.result, dict)
    assert got.result["status"] == "cancelled"


async def test_ambiguous_research_waits_for_the_clarification_tasks_result_delivers(
    app: FastAPI,
) -> None:
    replies = {"elicitation/create": {"action": "accept", "content": {"interpretation": "snake"}}}
    async with client(app, {"elicitation": {}}, replies) as wire:
        created = await rpc(
            wire,
            "tools/call",
            {
                "name": "simulate-research-query",
                "arguments": {"topic": "python", "ambiguous": True},
                "task": {},
            },
        )
        assert isinstance(created.result, dict)
        task_id = created.result["task"]["taskId"]
        await asyncio.sleep(TICK * 4)
        waiting = await rpc(wire, "tasks/get", {"taskId": task_id})
        result = await rpc(wire, "tasks/result", {"taskId": task_id})
        messages = await heard(wire)
    assert isinstance(waiting.result, dict)
    assert waiting.result["status"] == "input_required"
    statuses = [m["params"]["status"] for m in messages]
    assert statuses == [
        "working",
        "working",
        "working",
        "input_required",
        "input_required",
        "working",
        "working",
        "completed",
    ]
    assert isinstance(result.result, dict)
    report = result.result["content"][0]["text"]
    assert report.startswith("# Research Report: python (snake)\n")
    assert (
        'After receiving clarification ("snake"), the task resumed processing and completed.'
        in report
    )


@pytest.mark.parametrize(
    ("answer", "clarification"),
    [
        ({"action": "cancel"}, "User cancelled - using default interpretation"),
        ({"action": "accept", "content": {}}, "User accepted without selection"),
        ({"action": "nope"}, "technical (default - elicitation unavailable)"),
        ({"error": {"code": -1, "message": "no"}}, "technical (default - elicitation unavailable)"),
    ],
)
async def test_clarification_answers(app: FastAPI, answer: JsonObject, clarification: str) -> None:
    async with client(
        app, {"elicitation": {}}, {"elicitation/create": answer}, listen=False
    ) as wire:
        created = await rpc(
            wire,
            "tools/call",
            {
                "name": "simulate-research-query",
                "arguments": {"topic": "tides", "ambiguous": True},
                "task": {},
            },
        )
        assert isinstance(created.result, dict)
        result = await rpc(wire, "tasks/result", {"taskId": created.result["task"]["taskId"]})
    assert isinstance(result.result, dict)
    report = result.result["content"][0]["text"]
    assert report.startswith(f"# Research Report: tides ({clarification})\n")
    if "unavailable" in clarification:
        assert "**Note:** Elicitation failed and a default interpretation was used." in report


async def test_a_clarification_that_is_not_text_fails_the_task(app: FastAPI) -> None:
    replies = {"elicitation/create": {"action": "accept", "content": {"interpretation": True}}}
    async with client(app, {"elicitation": {}}, replies, listen=False) as wire:
        created = await rpc(
            wire,
            "tools/call",
            {
                "name": "simulate-research-query",
                "arguments": {"topic": "x", "ambiguous": True},
                "task": {},
            },
        )
        assert isinstance(created.result, dict)
        task_id = created.result["task"]["taskId"]
        result = await rpc(wire, "tasks/result", {"taskId": task_id})
        got = await rpc(wire, "tasks/get", {"taskId": task_id})
    assert result.error == {"code": -32603, "message": f"Task {task_id} has no result stored"}
    assert isinstance(got.result, dict)
    assert got.result["status"] == "failed"
    assert (
        got.result["statusMessage"] == "TypeError: state.clarification.includes is not a function"
    )


async def test_an_unanswered_clarification_times_out_to_the_default(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "REQUEST_TIMEOUT_S", 2.0)  # x TICK: a tenth of a second
    async with client(app, {"elicitation": {}}, listen=False) as wire:
        created = await rpc(
            wire,
            "tools/call",
            {
                "name": "simulate-research-query",
                "arguments": {"topic": "x", "ambiguous": True},
                "task": {},
            },
        )
        assert isinstance(created.result, dict)
        task_id = created.result["task"]["taskId"]
        for _ in range(40):
            await asyncio.sleep(TICK)
            got = await rpc(wire, "tasks/get", {"taskId": task_id})
            assert isinstance(got.result, dict)
            if got.result["status"] == "completed":
                break
    assert got.result["status"] == "completed"


async def test_tasks_expire_after_their_ttl(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "RESEARCH_TTL_MS", 300)
    async with client(app, listen=False) as wire:
        created = await rpc(
            wire,
            "tools/call",
            {"name": "simulate-research-query", "arguments": {"topic": "x"}, "task": {}},
        )
        assert isinstance(created.result, dict)
        task_id = created.result["task"]["taskId"]
        await asyncio.sleep(0.4)  # completed after 4 stages (0.2 s): the ttl starts again then
        still = await rpc(wire, "tasks/get", {"taskId": task_id})
        await asyncio.sleep(0.3)
        got = await rpc(wire, "tasks/get", {"taskId": task_id})
        listed = await rpc(wire, "tasks/list", {})
    assert isinstance(still.result, dict)
    assert still.result["status"] == "completed"
    assert got.error == {
        "code": -32602,
        "message": "MCP error -32602: Failed to retrieve task: Task not found",
    }
    assert listed.result == {"tasks": [], "_meta": {}}


async def test_task_listing_pages_by_ten(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        for index in range(12):
            await rpc(
                wire,
                "tools/call",
                {
                    "name": "simulate-research-query",
                    "arguments": {"topic": f"t{index}"},
                    "task": {},
                },
            )
        first = await rpc(wire, "tasks/list", {})
        assert isinstance(first.result, dict)
        cursor = first.result["nextCursor"]
        second = await rpc(wire, "tasks/list", {"cursor": cursor})
        bad = await rpc(wire, "tasks/list", {"cursor": "nope"})
        wrong = await rpc(wire, "tasks/list", {"cursor": 5})
    assert len(first.result["tasks"]) == 10
    assert list(first.result) == ["tasks", "nextCursor", "_meta"]
    assert cursor == first.result["tasks"][-1]["taskId"]
    assert isinstance(second.result, dict)
    assert len(second.result["tasks"]) == 2
    assert bad.error == {
        "code": -32602,
        "message": "MCP error -32602: Failed to list tasks: Invalid cursor: nope",
    }
    assert isinstance(wrong.error, dict)
    assert wrong.error["code"] == -32603


async def test_a_reset_stops_background_work(app: FastAPI) -> None:
    async with client(app) as wire:
        await tool(wire, "toggle-simulated-logging")
        runtime_of_app(app).reset()
        await asyncio.sleep(SIMULATION_S * 3)
        messages = await heard(wire)
    assert len(messages) <= 2  # the one sent at once, and at most one in flight


# -- roots and initialization ----------------------------------------------------------------------


ROOTS: Final[JsonObject] = {
    "roots": [{"uri": "file:///work", "name": "Work"}, {"uri": "file:///tmp"}]
}


async def test_roots_are_synced_after_initialization_and_on_changes(app: FastAPI) -> None:
    # the usual order: initialize (with notifications/initialized), then open the GET stream;
    # the server asks 350 ms later, by when the stream is open (recordings 000394, 000471)
    async with client(app, {"roots": {"listChanged": True}}, {"roots/list": ROOTS}) as wire:
        await asyncio.sleep(0.5)
        listed = await tool(wire, "get-roots-list")  # cached: no new request
        wire.on_request = replier({"roots/list": {"roots": []}})
        await wire.notify("notifications/roots/list_changed")
        await asyncio.sleep(0.1)
        after = await tool(wire, "get-roots-list")
        messages = await heard(wire)
    assert methods(messages) == [
        "roots/list",
        "notifications/message",
        "roots/list",
        "notifications/message",
    ]
    logs = [m["params"] for m in messages if m.get("method") == "notifications/message"]
    assert logs == [
        {
            "level": "info",
            "logger": "everything-server",
            "data": "Roots updated: 2 root(s) received from client",
        },
        {
            "level": "info",
            "logger": "everything-server",
            "data": "Roots updated: 0 root(s) received from client",
        },
    ]
    assert listed["content"][0]["text"].startswith(
        "Current MCP Roots (2 total):\n\n1. Work\n   URI: file:///work\n\n2. Unnamed Root\n"
        "   URI: file:///tmp\n\n"
    )
    assert after["content"][0]["text"].startswith(
        "The client supports roots but no roots are currently configured."
    )


async def test_a_refused_roots_request_is_answered_at_once(app: FastAPI) -> None:
    refused = {"roots/list": {"error": {"code": -32601, "message": "Method not found"}}}
    async with client(app, {"roots": {}}, refused) as wire:
        listed = await tool(wire, "get-roots-list")
        messages = await heard(wire, 0.5)
    assert listed["content"][0]["text"].startswith("The client supports roots but no roots")
    assert "notifications/message" not in methods(messages)  # only logged on the server (000471)
    assert all(pause < 1 for pause in runtime_of_app(app).pauses)


async def test_roots_the_schema_refuses_are_not_kept(app: FastAPI) -> None:
    replies = {"roots/list": {"roots": [{"uri": "https://not-a-file"}]}}
    async with client(app, {"roots": {}}, replies) as wire:
        listed = await tool(wire, "get-roots-list")
    assert listed["content"][0]["text"].startswith("The client supports roots but no roots")


async def test_without_a_get_stream_the_roots_request_is_lost(app: FastAPI) -> None:
    async with client(app, {"roots": {}}, {"roots/list": ROOTS}, listen=False) as wire:
        listed = await tool(wire, "get-roots-list")
    assert listed["content"][0]["text"].startswith("The client supports roots but no roots")
    assert 60.0 in runtime_of_app(app).pauses or any(p > 59 for p in runtime_of_app(app).pauses)


async def test_initialized_registers_one_tool_change_per_conditional_tool(app: FastAPI) -> None:
    # notifications/initialized sent once the GET stream is open, to see what it pushes
    capabilities: JsonObject = {
        "roots": {},
        "sampling": {},
        "elicitation": {"form": {}, "url": {}},
        "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
    }
    async with streaming_client(app) as http:
        wire = McpWireClient(f"{http.base_url}/mcp", http)
        await wire.initialize(capabilities=capabilities, announce=False)
        await wire.listen()
        await wire.notify("notifications/initialized")
        listed = await rpc(wire, "tools/list", {})
        messages = await heard(wire, 0.1)
        await wire.close()
    assert methods(messages).count("notifications/tools/list_changed") == 7
    assert isinstance(listed.result, dict)
    assert len(listed.result["tools"]) == 19
