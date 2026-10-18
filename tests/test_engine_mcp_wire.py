"""The MCP fake's wire, as learned: cancellation, stream ends, resumption, CORS, odd bodies."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx2
import pytest

from _mcp import live_server, mcp_app, mcp_model
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.model.mcp import McpToolModel, McpTransportModel
from fauxcall.runtime import ClientError, register_method, register_tool
from fauxcall.testing.asgi import streaming_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

SLOW: JsonObject = {"name": "slow", "inputSchema": {"type": "object"}}
POST_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


async def lines_of(lines: AsyncIterator[str], count: int) -> list[str]:
    """The next ``count`` non-empty lines of a stream's line reader (within 5 s)."""
    found: list[str] = []
    async with asyncio.timeout(5):
        while len(found) < count:
            line = await anext(lines)
            if line:
                found.append(line)
    return found


def cancelling(request_id: int) -> JsonValue:
    """The client's cancellation of request ``request_id``."""
    params: JsonObject = {"requestId": request_id, "reason": "user"}
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params}


def slow_fake(tmp_path: Path, transport: McpTransportModel) -> tuple[FastAPI, list[str]]:
    """A fake whose ``slow`` tool notifies, then takes its time; it notes how it ended."""
    tools = [McpToolModel(definition=SLOW, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport, tools=tools))
    ended: list[str] = []

    async def slow(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.notify("notifications/message", {"level": "info", "data": "started"})
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            ended.append("cancelled")
            raise
        ended.append("finished")
        return {"content": []}

    register_tool(app, "slow", slow)
    return app, ended


async def test_a_cancelled_call_gets_no_answer_and_its_stream_stays_open(tmp_path: Path) -> None:
    transport = McpTransportModel(response_mode="sse", keep_alive_seconds=0.02)
    app, ended = slow_fake(tmp_path, transport)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {**POST_HEADERS, "mcp-session-id": str(wire.session_id)}
        call = protocol.request(41, "tools/call", {"name": "slow", "arguments": {}})
        async with http.stream("POST", "/mcp", json=call, headers=headers) as stream:
            stream_lines = stream.aiter_lines()
            assert "started" in (await lines_of(stream_lines, 2))[-1]  # the notification came
            assert (await wire.post(cancelling(41))).status == 202
            after = await lines_of(stream_lines, 3)
        assert all(line.startswith(":") for line in after)  # keep-alives, never the answer
        assert ended == ["cancelled"]
        await wire.post({"jsonrpc": "2.0", "method": "notifications/cancelled"})  # no params
        await wire.close()


async def test_a_call_cancelled_before_it_streams(tmp_path: Path) -> None:
    transport = McpTransportModel(response_mode="sse", keep_alive_seconds=0.02)
    tools = [McpToolModel(definition=SLOW, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport, tools=tools))

    async def quiet(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        await asyncio.sleep(5)  # sends nothing before the answer
        return {"content": []}

    register_tool(app, "slow", quiet)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {**POST_HEADERS, "mcp-session-id": str(wire.session_id)}
        call = protocol.request(42, "tools/call", {"name": "slow", "arguments": {}})

        async def cancel_soon() -> None:
            await asyncio.sleep(0.005)  # before the first keep-alive
            await wire.post(cancelling(42))

        canceller = asyncio.ensure_future(cancel_soon())
        async with http.stream("POST", "/mcp", json=call, headers=headers) as stream:
            stream_lines = stream.aiter_lines()
            assert all(line.startswith(":") for line in await lines_of(stream_lines, 2))
        await canceller
        await wire.close()


async def test_json_answers_of_cancelled_calls_never_come(tmp_path: Path) -> None:
    app, ended = slow_fake(tmp_path, McpTransportModel(response_mode="json"))
    async with live_server(app) as base, httpx2.AsyncClient(base_url=base) as http:
        wire = McpWireClient(f"{base}/mcp", http)  # a real server: timeouts and hang-ups
        await wire.initialize()
        headers = {**POST_HEADERS, "mcp-session-id": str(wire.session_id)}
        call = protocol.request(43, "tools/call", {"name": "slow", "arguments": {}})
        pending = asyncio.ensure_future(http.post("/mcp", json=call, headers=headers, timeout=0.3))
        await asyncio.sleep(0.05)
        await wire.post(cancelling(43))
        with pytest.raises(httpx2.ReadTimeout):  # held open: the answer is dropped
            await pending
        assert ended == ["cancelled"]
        await wire.close()


async def test_the_fake_cancels_its_own_requests_that_time_out(tmp_path: Path) -> None:
    reason = "McpError: MCP error -32001: Request timed out"
    transport = McpTransportModel(response_mode="sse", timeout_cancel_reason=reason)
    tools = [McpToolModel(definition=SLOW, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport, tools=tools))

    async def ask(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        try:
            await ctx.ask_client("roots/list", within=0.05)
        except ClientError as error:
            return {"content": [{"type": "text", "text": error.message}]}
        return {"content": []}  # pragma: no cover - the client never answers

    register_tool(app, "slow", ask)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)  # reads the request, never answers
        await wire.initialize()
        answered = await wire.call("tools/call", {"name": "slow", "arguments": {}})
        sent = [json.loads(data) for _, _, data, _ in answered.sse_events if data.strip()]
        cancelled = next(m for m in sent if m.get("method") == "notifications/cancelled")
        assert cancelled["params"] == {"requestId": sent[0]["id"], "reason": reason}
        await wire.close()


async def test_ending_a_session_ends_its_stream(tmp_path: Path) -> None:
    app, _ = mcp_app(tmp_path, mcp_model(transport=McpTransportModel(get_stream="sse")))
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {"mcp-session-id": str(wire.session_id), "accept": "text/event-stream"}
        async with http.stream("GET", "/mcp", headers=headers) as stream:
            stream_lines = stream.aiter_lines()
            await http.delete("/mcp", headers={"mcp-session-id": str(wire.session_id)})
            async with asyncio.timeout(5):
                rest = [line async for line in stream_lines if line]
        assert rest == []  # it ended, with nothing more


async def test_the_fake_can_check_tool_calls_itself(tmp_path: Path) -> None:
    app, _ = mcp_app(tmp_path, mcp_model(transport=McpTransportModel(response_mode="json")))

    async def check(ctx: RequestContext, params: JsonObject) -> JsonValue:
        name = params.get("name")
        if name is not None and not isinstance(name, str):  # the real server's own wording
            return {"content": [{"type": "text", "text": f"name must be a string, got {name}"}]}
        return None  # the usual checks and the tools

    register_method(app, "tools/call", check)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        odd = await wire.call("tools/call", {"name": 5, "arguments": {}})
        assert "got 5" in json.dumps(odd.result)
        usual = await wire.call("tools/call", {"name": "nope", "arguments": {}})
        assert usual.error is not None  # left to FauxCall: an unknown tool
        await wire.close()


async def test_lone_surrogates_and_header_spelling(tmp_path: Path) -> None:
    refused: JsonValue = {"status": 415, "content_type": "text/plain", "body": "json only"}
    transport = McpTransportModel(response_mode="json", refusals={"content_type": refused})
    echo: JsonObject = {"name": "echo", "inputSchema": {"type": "object"}}
    app, _ = mcp_app(
        tmp_path,
        mcp_model(transport=transport, tools=[McpToolModel(definition=echo, behavior="computed")]),
    )

    async def echo_back(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        return {"content": [{"type": "text", "text": str(arguments["text"])}]}

    register_tool(app, "echo", echo_back)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {
            "mcp-session-id": str(wire.session_id),
            "accept": "application/json, text/event-stream",
            "content-type": "Application/JSON",  # any case: still JSON
        }
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        body += b'"params":{"name":"echo","arguments":{"text":"a\\ud800b"}}}'
        answer = await http.post("/mcp", content=body, headers=headers)
        assert answer.status_code == 200
        assert b"a\\ud800b" in answer.content  # escaped as JSON.stringify writes it
        await wire.close()


async def test_session_resumption_replays_every_stream(tmp_path: Path) -> None:
    second: JsonValue = {"status": 409, "content_type": "text/plain", "body": "one stream"}
    transport = McpTransportModel(
        response_mode="sse",
        get_stream="sse",
        sse_event_ids=True,
        resumption="session",
        keep_alive_seconds=0.02,
        refusals={"get_second_stream": second},
    )
    app, _ = slow_fake(tmp_path, transport)
    notify: JsonObject = {"name": "notify", "inputSchema": {"type": "object"}}

    async def notify_then_answer(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.notify("notifications/message", {"level": "info", "data": "one"})
        await asyncio.sleep(0.01)
        return {"content": []}

    app.state.fauxcall.mcp.model.tools.append(McpToolModel(definition=notify, behavior="computed"))
    register_tool(app, "notify", notify_then_answer)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {"mcp-session-id": str(wire.session_id), "accept": "text/event-stream"}
        async with http.stream("GET", "/mcp", headers=headers):  # the client's open stream
            answered = await wire.call("tools/call", {"name": "notify", "arguments": {}})
            first_id = answered.sse_events[0][3]
            resumed = {**headers, "last-event-id": str(first_id)}
            async with http.stream("GET", "/mcp", headers=resumed) as stream:
                stream_lines = stream.aiter_lines()
                assert stream.status_code == 200  # never refused as a second stream
                replayed = await lines_of(stream_lines, 4)
        data = [json.loads(line[5:]) for line in replayed if line.startswith("data:")]
        assert data[0] == answered.response  # the reply
        assert replayed[-1].startswith(":")  # then nothing more but keep-alives
        await wire.close()


async def test_the_servers_own_messages_in_its_member_order(tmp_path: Path) -> None:
    order = ["method", "params", "jsonrpc"]
    transport = McpTransportModel(
        response_mode="json", get_stream="sse", message_member_order=order
    )
    tools = [McpToolModel(definition=SLOW, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport, tools=tools))

    async def push(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.push("notifications/message", {"level": "info", "data": "pushed"})
        return {"content": []}

    register_tool(app, "slow", push)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {"mcp-session-id": str(wire.session_id), "accept": "text/event-stream"}
        async with http.stream("GET", "/mcp", headers=headers) as stream:
            stream_lines = stream.aiter_lines()
            await wire.call("tools/call", {"name": "slow", "arguments": {}})
            line = next(x for x in await lines_of(stream_lines, 2) if x.startswith("data:"))
        assert list(json.loads(line[5:])) == order
        await wire.close()


async def test_cors_answers(tmp_path: Path) -> None:
    cors: JsonValue = {
        "preflight": {
            "status": 204,
            "headers": [["access-control-allow-headers", "{{request_headers}}"]],
        },
        "headers": [["access-control-allow-origin", "{{origin}}"]],
        "headers_without_origin": [],
    }
    app, _ = mcp_app(tmp_path, mcp_model(transport=McpTransportModel(cors=cors)))
    async with streaming_client(app) as http:
        origin = {"origin": "https://inspector.test"}
        preflight = await http.options(
            "/mcp",
            headers={
                **origin,
                "access-control-request-method": "POST",
                "access-control-request-headers": "x-a",
            },
        )
        assert (preflight.status_code, preflight.headers["access-control-allow-headers"]) == (
            204,
            "x-a",
        )
        answered = await http.get("/__fauxcall/health", headers=origin)
        assert "access-control-allow-origin" not in answered.headers  # the admin API is left alone
        marked = await http.post("/mcp", headers=origin, json=protocol.request(1, "ping"))
        assert marked.headers["access-control-allow-origin"] == "https://inspector.test"
        bare = await http.options("/mcp")  # not a preflight: the app answers it
        assert bare.status_code == 405


async def test_watching_a_stream_sees_what_the_server_pushes(tmp_path: Path) -> None:
    from fauxcall.recording.mcp import _learn_from_stream, _sse_format  # noqa: PLC0415

    order = ["method", "params", "jsonrpc"]
    transport = McpTransportModel(
        response_mode="json", get_stream="sse", message_member_order=order, keep_alive_seconds=0.2
    )
    tools = [McpToolModel(definition=SLOW, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport, tools=tools))

    async def push(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.push("notifications/tools/list_changed")
        return {"content": []}

    register_tool(app, "slow", push)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        watching = asyncio.ensure_future(wire.watch_stream(2))
        await asyncio.sleep(0.05)  # the stream is open
        await wire.call("tools/call", {"name": "slow", "arguments": {}})
        watched, _ = await watching
        await wire.close()
    assert watched is not None
    learned = McpTransportModel()
    _learn_from_stream(learned, watched)
    assert learned.message_member_order == ["method", "jsonrpc"]  # as pushed (no params)
    assert learned.keep_alive == ": keep-alive"
    assert _sse_format(": only a comment\n\nid: 1\ndata: \n\n") == ([], None)


async def test_cors_with_only_a_preflight(tmp_path: Path) -> None:
    cors: JsonValue = {"preflight": {"status": 204, "headers": [["access-control-max-age", "600"]]}}
    app, _ = mcp_app(tmp_path, mcp_model(transport=McpTransportModel(cors=cors)))
    async with streaming_client(app) as http:
        plain = await http.post("/mcp", json=protocol.request(1, "ping"))
        assert not any(h.startswith("access-control-") for h in plain.headers)


async def test_resuming_from_a_priming_event(tmp_path: Path) -> None:
    transport = McpTransportModel(
        response_mode="sse",
        get_stream="sse",
        sse_event_ids=True,
        resumption="session",
        keep_alive_seconds=0.02,
        priming_since="2025-11-25",
        protocol_versions={v: v for v in protocol.KNOWN_VERSIONS},
    )
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport))
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http, protocol_version="2025-11-25")
        init = await wire.initialize()
        priming = init.sse_events[0][3]  # the handshake's priming event
        await wire.call("ping")  # primed too, then its answer
        headers = {
            "mcp-session-id": str(wire.session_id),
            "accept": "text/event-stream",
            "last-event-id": str(priming),
        }
        async with http.stream("GET", "/mcp", headers=headers) as stream:
            stream_lines = stream.aiter_lines()
            replayed = await lines_of(stream_lines, 5)
        assert "data: " in replayed  # the ping's priming event, replayed as it was
        await wire.close()
