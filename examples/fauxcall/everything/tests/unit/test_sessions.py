"""A client's session across resets, repeated initialization and protocol version negotiation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests
from wire_helpers import client, heard, methods, rpc, tool

from fauxcall_everything import tasks
from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI


async def rpc_initialize(wire: McpWireClient, version: str) -> Any:
    init = await wire.initialize(protocol_version=version)
    return init.result


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(tasks, "TICK", 0.05)
    return build_app(settings_for_tests(Settings))


async def test_a_reset_keeps_a_connected_clients_registered_tools(app: FastAPI) -> None:
    replies = {"roots/list": {"roots": [{"uri": "file:///a"}]}}
    async with client(app, {"roots": {}}, replies) as wire:
        await asyncio.sleep(0.5)  # the roots sync after initialization
        runtime_of_app(app).reset()
        created = await rpc(
            wire,
            "tools/call",
            {"name": "simulate-research-query", "arguments": {"topic": "x"}, "task": {}},
        )
        await wire.notify("notifications/roots/list_changed")  # its handler stays installed
        await asyncio.sleep(0.1)
        messages = await heard(wire)
    assert created.result["task"]["status"] == "working"
    others = [m for m in methods(messages) if m != "notifications/tasks/status"]
    assert others == ["roots/list", "notifications/message", "roots/list", "notifications/message"]


async def test_a_second_initialized_notification_changes_nothing(app: FastAPI) -> None:
    async with client(app, {"sampling": {}}) as wire:
        await wire.notify("notifications/initialized")
        await asyncio.sleep(0.1)
        messages = await heard(wire)
        listing = await rpc(wire, "tools/list", {})
    assert messages == []
    assert len(listing.result["tools"]) == 14


@pytest.mark.parametrize(
    ("asked", "answered"),
    [
        ("2024-10-07", "2024-10-07"),  # supported, as the recorded refusal lists it
        ("2025-11-25", "2025-11-25"),
        ("2099-01-01", "2025-11-25"),  # unsupported: the SDK's latest
    ],
)
async def test_protocol_version_negotiation(app: FastAPI, asked: str, answered: str) -> None:
    async with streaming_client(app) as http:
        wire = McpWireClient(f"{http.base_url}/mcp", http)
        await wire.initialize(protocol_version=asked)
        echoed = await tool(wire, "echo", {"message": "v"})
        await wire.close()
    assert wire.negotiated_version is None or wire.negotiated_version == answered
    assert echoed["content"][0]["text"] == "Echo: v"


async def test_an_older_supported_protocol_header_is_accepted(app: FastAPI) -> None:
    async with streaming_client(app) as http:
        wire = McpWireClient(f"{http.base_url}/mcp", http)
        init = await rpc_initialize(wire, "2024-10-07")
        assert init["protocolVersion"] == "2024-10-07"
        call = await wire.call("ping")
        await wire.close()
    assert call.status == 200
    assert call.result == {}


async def test_malformed_meta_is_refused_by_the_transport(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        metas: list[Any] = [
            {"progressToken": 1.5},
            {"progressToken": True},
            "x",
            {"io.modelcontextprotocol/related-task": {"taskId": 5}},
        ]
        for meta in metas:
            call = await wire.call("prompts/list", {"_meta": meta})
            assert call.status == 400
            assert call.error == {
                "code": -32700,
                "message": "Parse error: Invalid JSON-RPC message",
            }
        whole = await wire.call("ping", {"_meta": {"progressToken": 7.0}})
    assert whole.status == 200


async def test_requests_without_params(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        replies = [await rpc(wire, m) for m in ("tasks/get", "resources/read", "prompts/get")]
        listed = await rpc(wire, "tasks/list")
    for reply in replies:
        assert reply.error["code"] == -32603
        assert '"path": [\n      "params"\n    ]' in reply.error["message"]
    assert listed.result == {"tasks": [], "_meta": {}}
