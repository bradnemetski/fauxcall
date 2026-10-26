"""Protocol edges from the server's source and the review's probes: negotiation, session-less
requests, tool-list changes, the shared gzip registry, clarification timing, message order."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests
from mcp_session import session

from fauxcall_everything.app import build_app
from fauxcall_everything.client_tools import registered_tools
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fauxcall.types import JsonObject

HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
CLIENT = {"name": "edge", "version": "1"}
ALL: JsonObject = {
    "sampling": {},
    "elicitation": {"url": {}},
    "roots": {},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


def _initialize(version: str, **params: Any) -> dict[str, Any]:
    body = {"protocolVersion": version, "capabilities": {}, "clientInfo": CLIENT, **params}
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": body}


def _sse_messages(text: str) -> list[Any]:
    return [
        json.loads(line[5:].strip())
        for line in text.splitlines()
        if line.startswith("data:") and line[5:].strip()
    ]


@pytest.mark.parametrize(
    ("asked", "answered"),
    [("2024-10-07", "2024-10-07"), ("2099-01-01", "2025-11-25"), ("1.0", "2025-11-25")],
)
async def test_protocol_negotiation(asked: str, answered: str) -> None:
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        response = await http.post(
            "/mcp", headers={**HEADERS, "mcp-protocol-version": asked}, json=_initialize(asked)
        )
        assert response.status_code == 200
        assert _sse_messages(response.text)[-1]["result"]["protocolVersion"] == answered
        session_id = response.headers["mcp-session-id"]
        ping = await http.post(
            "/mcp",
            headers={**HEADERS, "mcp-session-id": session_id, "mcp-protocol-version": "2024-10-07"},
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        )
        assert ping.status_code == 200


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, "Bad Request: Server not initialized"),
        (
            _initialize("2025-06-18", clientInfo={"name": "x"}),
            "Bad Request: Server not initialized",
        ),
        (
            [_initialize("2025-06-18"), {"jsonrpc": "2.0", "id": 2, "method": "ping"}],
            "Invalid Request: Only one initialization request is allowed",
        ),
    ],
)
async def test_session_less_posts(body: Any, message: str) -> None:
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        response = await http.post("/mcp", headers={**HEADERS, "mcp-session-id": ""}, json=body)
        assert response.status_code == 400
        assert response.json()["error"]["message"] == message


async def test_session_less_refusals_keep_their_order() -> None:
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        accept = await http.post("/mcp", headers={"content-type": "application/json"}, content="{}")
        assert accept.status_code == 406
        broken = await http.post("/mcp", headers=HEADERS, content="{")
        assert broken.status_code == 400
        assert broken.json()["error"]["code"] == -32700
        head = await http.request("HEAD", "/mcp")
        assert head.status_code == 400
        assert head.content == b""


async def test_capability_tools_announce_themselves() -> None:
    assert registered_tools({}) == ["simulate-research-query"]
    assert len(registered_tools(ALL)) == 7
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        wire.on_request = replier({"roots/list": {"roots": []}})
        await wire.initialize(capabilities=ALL, announce=False)
        await wire.listen()
        await wire.notify("notifications/initialized")
        changed = await wire.heard(
            lambda m: isinstance(m, dict) and m.get("method") == "notifications/tools/list_changed",
            count=8,
            within=1.0,
            taken=set(),
        )
        assert len(changed) == 7
        await wire.notify("notifications/initialized")  # a second one changes nothing
        again = await wire.heard(
            lambda m: isinstance(m, dict) and m.get("method") == "notifications/tools/list_changed",
            count=8,
            within=0.5,
            taken=set(),
        )
        assert len(again) == 7
        await wire.close()


async def test_server_messages_keep_the_real_member_order() -> None:
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        params: JsonObject = {
            "name": "trigger-long-running-operation",
            "arguments": {"duration": 0, "steps": 1},
            "_meta": {"progressToken": "p"},
        }
        call = await wire.call("tools/call", params)
        progress = next(event for event in call.sse_events if "notifications/progress" in event[2])
        assert progress[2].startswith('{"method":"notifications/progress","params":')
        await wire.close()


async def test_gzip_registry_is_shared_between_sessions() -> None:
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        first, second = (McpWireClient("http://fake.test/mcp", http) for _ in range(2))
        for wire in (first, second):
            await wire.initialize()
            await wire.listen()
        arguments: JsonObject = {"name": "shared.gz", "data": "data:,one"}
        await first.call("tools/call", {"name": "gzip-file-as-resource", "arguments": arguments})
        await second.call("tools/call", {"name": "gzip-file-as-resource", "arguments": arguments})
        lost = await first.call("resources/read", {"uri": "demo://resource/session/shared.gz"})
        refused: Any = lost.response
        assert refused["error"]["code"] == -32602
        kept = await second.call("resources/read", {"uri": "demo://resource/session/shared.gz"})
        assert isinstance(kept.response, dict)
        assert "result" in kept.response

        def changed(message: object) -> bool:
            return isinstance(message, dict) and message.get("method") == (
                "notifications/resources/list_changed"
            )

        heard_first = await first.heard(changed, count=3, within=0.5, taken=set())
        heard_second = await second.heard(changed, count=3, within=0.5, taken=set())
        assert (len(heard_first), len(heard_second)) == (2, 1)
        for wire in (first, second):
            await wire.close()


async def test_unanswered_clarification_times_out_from_the_queue_and_stays_queued() -> None:
    elicit: JsonObject = {"elicitation": {}}
    async with session(elicit, listen=True, frozen=True) as client:
        created = await client.tool(
            "simulate-research-query", {"topic": "python", "ambiguous": True}, task={}
        )
        task_id = created["task"]["taskId"]
        await client.advance(1.0)
        await client.advance(1.0)
        await client.advance(60.0)
        task = await client.call("tasks/get", {"taskId": task_id})
        assert task["statusMessage"].startswith('Continuing with interpretation: "technical')
        await client.advance(1.0)
        await client.advance(1.0)
        client.answer(
            {"elicitation/create": {"action": "accept", "content": {"interpretation": "x"}}}
        )
        messages = await client.messages("tasks/result", {"taskId": task_id})
        assert messages[0]["method"] == "elicitation/create"  # the stale request, delivered late
        final: Any = messages[-1]
        report = final["result"]["content"][0]["text"]
        assert "(technical (default - elicitation unavailable))" in report
        assert await client.heard("notifications/cancelled", within=0.3) == []


async def test_delivered_clarification_does_not_hold_the_result() -> None:
    async def silent(request: JsonObject) -> Any:
        del request  # a client that never answers

    async with session({"elicitation": {}}, frozen=True) as client:
        client.answer(silent)
        created = await client.tool(
            "simulate-research-query", {"topic": "python", "ambiguous": True}, task={}
        )
        task_id = created["task"]["taskId"]
        await client.advance(1.0)
        await client.advance(1.0)
        waiting = asyncio.ensure_future(client.messages("tasks/result", {"taskId": task_id}))
        await asyncio.sleep(0.2)
        cancelled = await client.call("tasks/cancel", {"taskId": task_id})
        assert cancelled["status"] == "cancelled"
        await client.advance(1.0)
        messages = await asyncio.wait_for(waiting, 2)
        assert messages[0]["method"] == "elicitation/create"
        assert messages[-1]["error"]["message"] == f"Task {task_id} has no result stored"
        assert all(m.get("method") != "notifications/cancelled" for m in messages)


@pytest.mark.parametrize(
    "capabilities",
    [
        {"roots": 5},
        {"elicitation": None},
        {"roots": {"listChanged": "yes"}},
        {"sampling": {"tools": 1}},
        {"experimental": {"x": 1}},
        {"tasks": []},
        {"elicitation": {"form": {"applyDefaults": "no"}}},
    ],
)
async def test_initialize_capabilities_must_match_the_schema(capabilities: Any) -> None:
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        body = _initialize("2025-06-18", capabilities=capabilities)
        response = await http.post("/mcp", headers=HEADERS, json=body)
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "Bad Request: Server not initialized"


async def test_related_task_is_looked_up_before_the_schema() -> None:
    meta: JsonObject = {"io.modelcontextprotocol/related-task": {"taskId": "nope"}}
    async with session() as client:
        call = await client.call("tools/call", {"name": 5, "_meta": meta})
        assert call["message"] == "MCP error -32602: Task not found: nope"
        listing = await client.call("tools/list", {"cursor": 5, "_meta": meta})
        assert listing["message"] == "MCP error -32602: Task not found: nope"
        ping = await client.call("ping", {"_meta": meta})
        assert ping["code"] == -32602
