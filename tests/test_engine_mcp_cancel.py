"""What a call the client cancels does: stop, or finish with only its answer dropped."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from _mcp import mcp_app, mcp_model
from fauxcall.errors import RecordingError
from fauxcall.mcpwire.client import CANCEL_REASON, McpWireClient, RpcCall
from fauxcall.model.mcp import McpToolModel, McpTransportModel
from fauxcall.runtime import register_tool
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.client import McpClient

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

SAVE: JsonObject = {"name": "save", "inputSchema": {"type": "object"}}


def saving_fake(
    tmp_path: Path, on_cancel: str | None, *, checks_signal: bool = False
) -> tuple[FastAPI, list[str]]:
    """A fake whose ``save`` tool takes 0.2 s, then saves: ``saved`` notes what it did."""
    transport = McpTransportModel.model_validate(
        {"response_mode": "sse", "keep_alive_seconds": 0.05, "on_cancel": on_cancel}
    )
    tools = [McpToolModel(definition=SAVE, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport, tools=tools))
    saved: list[str] = []

    async def save(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.notify("notifications/message", {"level": "info", "data": "saving"})
        await asyncio.sleep(0.2)
        ctx.notify("notifications/message", {"level": "info", "data": "wrote it"})
        if checks_signal and ctx.cancelled:  # a tool that checks its abort signal
            saved.append("abandoned")
            return {"content": []}
        saved.append("cancelled" if ctx.cancelled else "saved")
        return {"content": [{"type": "text", "text": "saved"}]}

    register_tool(app, "save", save)
    return app, saved


async def cancelled_save(app: FastAPI) -> RpcCall:
    """Call ``save``, cancel it 50 ms in, and read on until its work has ended."""
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        wire.cancel_linger_s = 0.3
        await wire.initialize()
        call = await wire.call("tools/call", {"name": "save", "arguments": {}}, cancel_after=0.05)
        assert call.cancelled_after_ms is not None
        assert call.cancelled_after_ms >= 50
        await asyncio.sleep(0.1)
        await wire.close()
        return call


def logged(call: RpcCall) -> list[str]:
    """The log messages a call's stream carried."""
    return [
        str(m["params"]["data"])  # type: ignore[call-overload,index]
        for m in call.messages
        if isinstance(m, dict) and "params" in m
    ]


@pytest.mark.parametrize(
    ("on_cancel", "outcome"), [(None, []), ("stop", []), ("finish", ["cancelled"])]
)
async def test_cancelled_work_stops_or_finishes(
    tmp_path: Path, on_cancel: str | None, outcome: list[str]
) -> None:
    app, saved = saving_fake(tmp_path, on_cancel)
    call = await cancelled_save(app)
    assert call.response is None  # never answered either way
    assert saved == outcome  # finished: the write happened, and the handler knew
    # what it sends as it goes still reaches the client, as the real TypeScript SDK's progress does
    assert logged(call) == ["saving", "wrote it"] if outcome else ["saving"]


async def test_a_finishing_handler_can_stop_where_the_real_tool_checks(tmp_path: Path) -> None:
    app, saved = saving_fake(tmp_path, "finish", checks_signal=True)
    assert (await cancelled_save(app)).response is None
    assert saved == ["abandoned"]


async def test_finished_work_that_fails_is_nobodys_to_report(tmp_path: Path) -> None:
    transport = McpTransportModel(response_mode="sse", on_cancel="finish")
    tools = [McpToolModel(definition=SAVE, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport, tools=tools))

    async def failing(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        await asyncio.sleep(0.2)
        raise RuntimeError("the disk is full")

    register_tool(app, "save", failing)
    assert (
        await cancelled_save(app)
    ).response is None  # and no "exception never retrieved" warning


async def test_a_call_answered_before_the_cancel_is_answered(tmp_path: Path) -> None:
    app, saved = saving_fake(tmp_path, "stop")
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        call = await wire.call("tools/call", {"name": "save", "arguments": {}}, cancel_after=5)
        assert call.cancelled_after_ms is None  # the answer came first: nothing was cancelled
        assert call.result == {"content": [{"type": "text", "text": "saved"}]}
        await wire.close()
    assert saved == ["saved"]


async def test_parity_logs_the_answer_a_cancelled_call_never_got(tmp_path: Path) -> None:
    app, saved = saving_fake(tmp_path, "finish")
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        wire.cancel_linger_s = 0.1
        client = McpClient("fake", wire, allow_writes=False, mutating=lambda _name: False)
        assert await client.call_tool("save", cancel_after=0.05, notifications=True) is None
        await asyncio.sleep(0.3)
        await client.close()
    answer, sent = client.log.calls
    assert answer.observation.status == 204
    assert sent.path == "tools/call:save:notifications"  # what it sent before the cancel
    assert saved == ["cancelled"]


async def test_the_legacy_transport_cannot_cancel(tmp_path: Path) -> None:
    app, _ = saving_fake(tmp_path, "stop")
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http, transport="sse")
        with pytest.raises(RecordingError, match="Streamable HTTP"):
            await wire.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, cancel_after=0.1)


def test_the_cancel_says_who_cancelled() -> None:
    assert "fauxcall" in CANCEL_REASON


def test_servers_with_writing_tools_must_decide() -> None:
    read_only: JsonObject = {**SAVE, "annotations": {"readOnlyHint": True}}
    model = mcp_model(tools=[McpToolModel(definition=read_only)])
    assert model.undecided() == []  # nothing a cancel could leave half done
    assert model.mcp is not None
    model.mcp.tools.append(McpToolModel(definition=SAVE))
    assert [u.split(" ")[0] for u in model.undecided()] == ["mcp.transport.on_cancel"]
    model.mcp.transport.on_cancel = "finish"
    assert model.undecided() == []


def test_only_mcp_calls_are_ever_cancelled(tmp_path: Path) -> None:
    from starlette.requests import Request  # noqa: PLC0415

    from _engine import harness  # noqa: PLC0415

    fake = harness(tmp_path)
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    ctx = fake.runtime.new_context(Request(scope), None)
    assert ctx.cancelled is False  # a REST request has no client to cancel it


@pytest.mark.parametrize(("on_cancel", "outcome"), [("stop", []), ("finish", ["saved"])])
async def test_a_client_that_hangs_up(tmp_path: Path, on_cancel: str, outcome: list[str]) -> None:
    app, saved = saving_fake(tmp_path, on_cancel)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {
            "accept": "application/json, text/event-stream",
            "mcp-session-id": str(wire.session_id),
        }
        call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "save"}}
        async with http.stream("POST", "/mcp", json=call, headers=headers) as stream:
            async for line in stream.aiter_lines():
                if "saving" in line:
                    break  # and hang up, without cancelling
        await asyncio.sleep(0.3)
        await wire.close()
    assert saved == outcome  # the work ends with the stream, or runs on where it finishes
