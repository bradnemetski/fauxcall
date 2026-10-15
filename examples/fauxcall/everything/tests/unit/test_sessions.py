"""What the fake sends outside calls, and what it keeps per client (in-process sessions)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING

import pytest
from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests
from mcp_session import answer, connect, dig, text_of, tool

from fauxcall_everything import handlers
from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from fauxcall.types import JsonObject, JsonValue

ROOTS: JsonObject = {"roots/list": {"roots": [{"uri": "file:///work", "name": "work"}]}}


@pytest.fixture
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


async def _heard(wire: McpWireClient, settle: float = 0.05) -> list[JsonObject]:
    """Everything the GET stream carried so far."""
    await asyncio.sleep(settle)
    stream = await wire.stop_listening()
    assert stream is not None
    messages = [json.loads(data) for _, _, data, _ in stream.sse_events if data]
    return [m for m in messages if isinstance(m, dict)]


async def test_simulated_logging_respects_the_level(app: FastAPI) -> None:
    async with connect(app, listen=True) as wire:
        session = wire.session_id
        assert await answer(wire, "logging/setLevel", {"level": "emergency"}) == {}
        started = await tool(wire, "toggle-simulated-logging")
        assert text_of(started).startswith(
            f"Started simulated, random-leveled logging for session {wire.session_id} "
        )
        stopped = await tool(wire, "toggle-simulated-logging")
        assert text_of(stopped) == f"Stopped simulated logging for session {wire.session_id}"
        heard = await _heard(wire)
    logs = [m["params"] for m in heard if m.get("method") == "notifications/message"]
    assert all(dig(log, "level") == "emergency" for log in logs)
    assert all(str(dig(log, "data")).endswith(f" - SessionId {session}") for log in logs)


async def test_simulated_logging_sends_at_once(app: FastAPI) -> None:
    async with connect(app, listen=True) as wire:
        await tool(wire, "toggle-simulated-logging")
        heard = await _heard(wire)
        await tool(wire, "toggle-simulated-logging")
    assert [m["method"] for m in heard] == ["notifications/message"]


async def test_subscriptions_and_updates(app: FastAPI) -> None:
    async with connect(app, listen=True) as wire:
        session = wire.session_id
        assert (
            await answer(wire, "resources/subscribe", {"uri": "demo://resource/dynamic/text/1"})
            == {}
        )
        assert await answer(wire, "resources/subscribe", {"uri": "demo://a"}) == {}
        assert await answer(wire, "resources/unsubscribe", {"uri": "demo://a"}) == {}
        assert await answer(wire, "resources/unsubscribe", {"uri": "demo://never"}) == {}
        on = await tool(wire, "toggle-subscriber-updates")
        assert "Client will receive updates for any resources the it is subscribed to." in text_of(
            on
        )
        heard = await _heard(wire)
        off = await tool(wire, "toggle-subscriber-updates")
        assert text_of(off) == f"Stopped simulated resource updates for session {wire.session_id}"
    data = [dig(m, "params", "data") for m in heard if m["method"] == "notifications/message"]
    assert (
        data[0] == "Received Subscribe Resource request for URI: demo://resource/dynamic/text/1 "
        f"from session {session}"
    )
    assert data[2] == f"Received Unsubscribe Resource request: demo://a from session {session}"
    updates = [m["params"] for m in heard if m["method"] == "notifications/resources/updated"]
    assert updates == [{"uri": "demo://resource/dynamic/text/1"}]


@contextlib.asynccontextmanager
async def _roots_client(app: FastAPI, replies: JsonObject) -> AsyncIterator[McpWireClient]:
    """A roots-capable client that opens its GET stream before announcing itself initialized."""
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize(capabilities={"roots": {"listChanged": True}}, announce=False)
        wire.on_request = replier(replies)
        await wire.listen()
        await wire.notify("notifications/initialized")
        try:
            yield wire
        finally:
            await wire.close()


async def test_roots_are_synced_after_initialize(app: FastAPI) -> None:
    async with _roots_client(app, ROOTS) as wire:
        await asyncio.sleep(0.05)
        listed = await tool(wire, "get-roots-list")
        assert text_of(listed).startswith(
            "Current MCP Roots (1 total):\n\n1. work\n   URI: file:///work"
        )
        wire.on_request = replier({"roots/list": {"roots": []}})
        await wire.notify("notifications/roots/list_changed")
        heard = await _heard(wire)
        emptied = await tool(wire, "get-roots-list")
    assert text_of(emptied) == handlers.NO_ROOTS
    requests = [m for m in heard if m.get("method") == "roots/list"]
    assert [r["id"] for r in requests] == [0, 1]  # one at the sync, one after list_changed
    logs = [dig(m, "params", "data") for m in heard if m.get("method") == "notifications/message"]
    assert logs == [
        "Roots updated: 1 root(s) received from client",
        "Roots updated: 0 root(s) received from client",
    ]


async def test_roots_list_changed_before_the_sync_is_ignored(app: FastAPI) -> None:
    """The server watches for roots/list_changed only from its sync on (after initialized)."""
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize(capabilities={"roots": {}}, announce=False)
        wire.on_request = replier(ROOTS)
        await wire.listen()
        await wire.notify("notifications/roots/list_changed")  # no handler yet: nothing asked
        await asyncio.sleep(0.05)
        await wire.notify("notifications/initialized")  # the sync asks, once
        heard = await _heard(wire)
        await wire.close()
    assert [m.get("method") for m in heard] == [
        "notifications/tools/list_changed",  # get-roots-list and simulate-research-query
        "notifications/tools/list_changed",  # registered for this client (a stream already open)
        "roots/list",
        "notifications/message",
    ]
    assert dig(heard[3], "params", "data") == "Roots updated: 1 root(s) received from client"


async def test_roots_without_a_stream_wait_for_the_timeout(app: FastAPI) -> None:
    runtime = runtime_of_app(app)
    async with connect(app, capabilities={"roots": {}}, replies=ROOTS) as wire:
        await asyncio.sleep(0.05)
        runtime.pauses.clear()
        listed = await tool(wire, "get-roots-list")
    assert text_of(listed) == handlers.NO_ROOTS
    assert any(55 < pause <= 60 for pause in runtime.pauses)


async def test_gzip_resources_move_between_clients(app: FastAPI) -> None:
    async with connect(app, listen=True) as first, connect(app) as second:
        made = await tool(
            first, "gzip-file-as-resource", {"name": "shared.gz", "data": "data:,one"}
        )
        assert dig(made, "content", 0, "uri") == "demo://resource/session/shared.gz"
        await tool(second, "gzip-file-as-resource", {"name": "shared.gz", "data": "data:,two"})
        refused = await answer(
            first, "resources/read", {"uri": "demo://resource/session/shared.gz"}
        )
        heard = await _heard(first)
        listed = await answer(second, "resources/list", {})
    assert refused == {
        "code": -32602,
        "message": "MCP error -32602: Resource demo://resource/session/shared.gz not found",
    }
    assert [m["method"] for m in heard] == ["notifications/resources/list_changed"] * 2
    assert listed["resources"][-1]["name"] == "shared.gz"  # type: ignore[index, call-overload]


async def test_gzip_refuses_huge_and_broken_data(app: FastAPI) -> None:
    """Data over gzip's 10 MB limit can't reach it: the transport takes 4 MiB at most."""
    async with connect(app) as wire:
        huge = await tool(wire, "gzip-file-as-resource", {"data": "data:," + "a" * 4 * 2**20})
        broken = await tool(wire, "gzip-file-as-resource", {"data": "data:;base64,@@@"})
        spaced = await tool(
            wire, "gzip-file-as-resource", {"name": "my file.gz", "data": "data:,x"}
        )
        unreadable = await answer(
            wire, "resources/read", {"uri": "demo://resource/session/my file.gz"}
        )
    assert huge == {
        "code": -32000,
        "message": "Payload Too Large: Request body must not exceed 4194304 bytes",
    }
    assert text_of(broken) == "fetch failed"
    assert dig(spaced, "content", 0, "uri") == "demo://resource/session/my file.gz"
    assert unreadable == {
        "code": -32602,
        "message": "MCP error -32602: Resource demo://resource/session/my%20file.gz not found",
    }


async def test_url_elicitation_refuses_bad_replies(app: FastAPI) -> None:
    capabilities: JsonObject = {"elicitation": {"form": {}, "url": {}}}
    async with connect(
        app, capabilities=capabilities, replies={"elicitation/create": {"action": "later"}}
    ) as wire:
        refused = await tool(wire, "trigger-url-elicitation", {"url": " https://example.com/x "})
    assert refused["isError"] is True
    assert '"invalid_value"' in text_of(refused)


ASYNC: JsonObject = {
    "sampling": {},
    "elicitation": {},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


async def test_async_tools_give_up_after_their_polls(app: FastAPI) -> None:
    working: JsonObject = {
        "sampling/createMessage": {"task": {"taskId": "c1", "status": "working"}},
        "elicitation/create": {"task": {"taskId": "c2", "status": "input_required"}},
        "tasks/get": {"taskId": "c", "status": "input_required"},
    }
    async with connect(app, capabilities=ASYNC, replies=working) as wire:
        sampling = await tool(wire, "trigger-sampling-request-async", {"prompt": "p"})
        elicitation = await tool(wire, "trigger-elicitation-request-async")
    assert text_of(sampling).startswith(
        "[TIMEOUT] Task timed out after 60 poll attempts\n\n"
        "Progress:\nTask created: c1\nPoll 1: input_required"
    )
    lines = text_of(elicitation).split("\n")
    assert lines[0] == "[TIMEOUT] Task timed out after 600 poll attempts"
    polls = [line for line in lines if line.startswith("Poll ")]
    assert polls[:3] == [
        "Poll 1: input_required",
        "Poll 10: input_required",
        "Poll 20: input_required",
    ]
    assert len(polls) == 61


async def test_async_tools_report_client_failures(app: FastAPI) -> None:
    replies: JsonObject = {
        "sampling/createMessage": {"task": {"taskId": "c1", "status": "working"}},
        "elicitation/create": {"task": {"taskId": "c2", "status": "working"}},
        "tasks/get": {"status": "completed"},
    }
    async with connect(app, capabilities=ASYNC, replies=replies) as wire:
        sampling = await tool(wire, "trigger-sampling-request-async", {"prompt": "p"})
        elicitation = await tool(wire, "trigger-elicitation-request-async")
        wire.on_request = replier(
            {
                "elicitation/create": {"task": {"taskId": "c3", "status": "working"}},
                "tasks/get": {"status": "completed"},
                "tasks/result": {"action": "cancel"},
            }
        )
        cancelled = await tool(wire, "trigger-elicitation-request-async")
    assert text_of(sampling) == "MCP error -32601: Method not found"
    assert text_of(elicitation) == "MCP error -32601: Method not found"
    assert text_of(cancelled) == "[CANCELLED] User cancelled the elicitation dialog."


async def test_tasks_of_a_session(app: FastAPI) -> None:
    async with connect(app) as wire:
        created = await tool(wire, "simulate-research-query", {"topic": "tides"}, task={"ttl": 5})
        task: JsonValue = created["task"]
        assert isinstance(task, dict)
        task_id = task["taskId"]
        cancelled = await answer(wire, "tasks/cancel", {"taskId": task_id})
        result = await answer(wire, "tasks/result", {"taskId": task_id})
        missing = await answer(wire, "tasks/result", {"taskId": "0" * 32})
        bad_ref = await answer(
            wire, "completion/complete", {"ref": "prompt", "argument": {"name": "x", "value": ""}}
        )
        no_arg = await answer(
            wire,
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": handlers.TEXT_TEMPLATE},
                "argument": {"name": "other", "value": "1"},
            },
        )
    assert cancelled["status"] == "cancelled"  # type: ignore[index, call-overload]
    assert result == {"code": -32603, "message": f"Task {task_id} has no result stored"}
    assert missing == {"code": -32602, "message": f"MCP error -32602: Task not found: {'0' * 32}"}
    assert bad_ref["code"] == -32603  # type: ignore[index, call-overload]
    assert no_arg == {"completion": {"values": [], "hasMore": False}}


async def test_tasks_result_once_the_task_expired(app: FastAPI) -> None:
    runtime = runtime_of_app(app)
    async with connect(app) as wire:
        created = await tool(
            wire, "simulate-research-query", {"topic": "tides", "ambiguous": True}, task={}
        )
        task_id = created["task"]["taskId"]  # type: ignore[index, call-overload]
        clock = runtime.clocks.for_session("default")
        clock.advance(400)
        gone = await answer(wire, "tasks/get", {"taskId": task_id})
        listed = await answer(wire, "tasks/list", {})
    assert gone == {
        "code": -32602,
        "message": "MCP error -32602: Failed to retrieve task: Task not found",
    }
    assert listed == {"tasks": [], "_meta": {}}


async def test_session_resource_completion_is_empty(app: FastAPI) -> None:
    async with connect(app) as wire:
        await tool(wire, "gzip-file-as-resource", {"name": "c.gz", "data": "data:,x"})
        done = await answer(
            wire,
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "demo://resource/session/c.gz"},
                "argument": {"name": "x", "value": ""},
            },
        )
    assert done == {"completion": {"values": [], "hasMore": False}}


def test_local_time_spelling() -> None:
    midnight = 1_792_036_800_000_000_000  # 2026-10-15T04:00:00Z = 00:00 in New York
    assert handlers.local_time(midnight) == "12:00:00 AM"
    assert handlers.local_time(midnight + 13 * 3600 * 10**9 + 5 * 10**9) == "1:00:05 PM"
    assert handlers.node_timer_ms(0) == 1.0
    assert handlers.node_timer_ms(float("nan")) == 1.0
    assert handlers.node_timer_ms(2.0**40) == 1.0
    assert handlers.node_timer_ms(500.0) == 500.0


async def test_a_reset_ends_what_a_client_left_running(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated logging and resource updates stop with a reset of the fake (a fresh server)."""
    monkeypatch.setattr(handlers, "SIMULATION_PERIOD", 0.01)
    runtime = runtime_of_app(app)
    async with connect(app, listen=True) as wire:
        assert await answer(wire, "resources/subscribe", {"uri": "demo://resource/static/x"}) == {}
        await tool(wire, "toggle-simulated-logging")
        await tool(wire, "toggle-subscriber-updates")
        await asyncio.sleep(0.05)
        assert runtime.mcp is not None
        loops = set(runtime.mcp.background[wire.session_id or ""])
        assert len(loops) == 2
        runtime.reset()
        reset_ns = time.time_ns()
        await asyncio.wait(loops, timeout=1)
        stream = await wire.stop_listening()
    assert all(loop.done() for loop in loops)
    assert stream is not None
    sent = [(stamp, json.loads(data)["method"]) for stamp, _, data, _ in stream.sse_events if data]
    assert {method for _, method in sent} == {
        "notifications/message",
        "notifications/resources/updated",
    }
    since_reset = (reset_ns - stream.started_ns) / 1e6
    assert all(stamp < since_reset + 5 for stamp, _ in sent)  # nothing sent after the reset


async def test_subscriptions_are_one_map_for_every_client(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client's updates follow the order URIs were first subscribed, by any client."""
    monkeypatch.setattr(handlers, "SIMULATION_PERIOD", 0.01)
    first, later = "demo://resource/static/document/a.md", "demo://resource/static/document/b.md"
    async with connect(app) as other, connect(app, listen=True) as wire:
        assert await answer(other, "resources/subscribe", {"uri": first}) == {}
        assert await answer(wire, "resources/subscribe", {"uri": later}) == {}
        assert await answer(wire, "resources/subscribe", {"uri": first}) == {}
        await tool(wire, "toggle-subscriber-updates")
        heard = await _heard(wire)
        await tool(wire, "toggle-subscriber-updates")
    updated = [
        dig(m, "params", "uri") for m in heard if m["method"] == "notifications/resources/updated"
    ]
    assert updated[:2] == [first, later]


def _related(task_id: str) -> JsonObject:
    return {"_meta": {"io.modelcontextprotocol/related-task": {"taskId": task_id}}}


async def test_a_related_task_is_looked_up_first(app: FastAPI) -> None:
    """A request naming a task the client doesn't have is refused before it runs."""
    unknown = _related("nope")
    refused = {"code": -32602, "message": "MCP error -32602: Task not found: nope"}
    async with connect(app) as wire:
        assert await answer(wire, "logging/setLevel", {"level": "info", **unknown}) == refused
        assert await answer(wire, "completion/complete", unknown) == refused  # before its schema
        assert await tool(wire, "echo", {"message": "x"}, **unknown) == refused
        created = await tool(wire, "simulate-research-query", {"topic": "tides"}, task={})
        task_id = dig(created, "task", "taskId")
        known = _related(str(task_id))
        assert await answer(wire, "logging/setLevel", {"level": "info", **known}) == {}
        empty = _related("")
        assert await answer(wire, "logging/setLevel", {"level": "info", **empty}) == {}
        noted = await wire.notify("notifications/roots/list_changed", unknown)
    assert noted.status == 202  # notifications are not looked up
