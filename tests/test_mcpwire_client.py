from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from fauxcall import jsonfmt
from fauxcall.errors import RecordingError
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient, RpcCall
from fauxcall.model import McpTransportModel
from fauxcall.testing.asgi import streaming_client
from test_mcpwire_transport import fake_app, fake_model, sdk_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from fauxcall.types import JsonValue


def j(value: object) -> Any:
    """View decoded JSON loosely."""
    return value


def _session(wire: McpWireClient) -> str | None:
    """The session id, read through a call so type narrowing doesn't stick to it."""
    return wire.session_id


def _call(**overrides: Any) -> RpcCall:
    fields: dict[str, Any] = {
        "method": "x",
        "sent": None,
        "status": 200,
        "request_headers": [],
        "response_headers": [("Content-Type", "application/json"), ("X-A", "1")],
        "content_type": "application/json",
        "body": "",
        "messages": [],
        "response": None,
        "started_ns": 0,
        "elapsed_ms": 0.0,
    }
    fields.update(overrides)
    return RpcCall(**fields)


def test_rpc_call_accessors() -> None:
    call = _call(response={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    assert call.header("x-a") == "1"
    assert call.header("missing") is None
    assert call.result == {"ok": True}
    assert call.error is None
    failed = _call(response={"jsonrpc": "2.0", "id": 1, "error": {"code": 1}})
    assert failed.error == {"code": 1}
    assert _call().result is None
    assert _call().error is None


# -- against FauxCall's fake -----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["json", "sse"])
async def test_fake_session_listing_and_errors(tmp_path: Path, mode: str) -> None:
    transport = McpTransportModel(
        response_mode=mode,  # type: ignore[arg-type]
        get_stream="sse",
        delete_status=204,
        sse_event_ids=True,
        protocol_versions={"2025-03-26": "2025-03-26"},
    )
    app = fake_app(tmp_path, fake_model(transport=transport, page_size=2))
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http, headers={"X-Trace": "t1"})
        init = await wire.initialize()
        assert wire.session_id
        assert wire.negotiated_version == "2025-06-18"
        assert j(init.result)["serverInfo"] == {"name": "Demo", "version": "1.0.0"}
        assert ("x-trace", "t1") in [(k.lower(), v) for k, v in init.request_headers]
        if mode == "sse":
            assert init.content_type is not None
            assert "event-stream" in init.content_type
            assert init.sse_events
            assert init.sse_events[0][1] == "message"
            assert init.sse_events[0][3] is not None  # the fake numbers its events
        else:
            assert init.content_type == "application/json"
            assert init.sse_events == []
        pages: list[int] = []

        async def count() -> None:
            pages.append(len(pages))

        tools, calls = await wire.list_all("tools/list", "tools", before_page=count)
        assert [t["name"] for t in j(tools)] == ["echo", "delete_item", "whoami"]
        assert len(calls) == 2
        assert pages == [0, 1]
        assert j(calls[1].sent)["params"] == {"cursor": j(calls[0].result)["nextCursor"]}
        headers = dict(calls[0].request_headers)
        assert headers[protocol.SESSION_HEADER] == wire.session_id
        assert headers[protocol.PROTOCOL_HEADER] == "2025-06-18"
        prompts, _ = await wire.list_all("prompts/list", "prompts")
        assert prompts == [{"name": "summarize", "description": "Summarize a topic."}]
        echo = await wire.call("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        assert j(echo.result)["content"][0]["text"] == "hi"
        assert j(echo.response)["id"] == j(echo.sent)["id"]
        unknown = await wire.call("tools/call", {"name": "nope", "arguments": {}})
        assert j(unknown.error)["code"] == protocol.INVALID_PARAMS
        missing = await wire.call("does/not/exist")
        assert j(missing.error)["code"] == protocol.METHOD_NOT_FOUND
        accepted = await wire.notify("notifications/progress", {"progress": 1})
        assert (accepted.status, accepted.response, accepted.messages) == (202, None, [])
        assert await wire.get_stream_status() == (200, "text/event-stream; charset=utf-8")
        no_session = await wire.post(protocol.request(99, "tools/list"), include_session=False)
        assert no_session.status == 400
        assert j(no_session.response)["error"]["code"] == protocol.INVALID_REQUEST  # id is null
        assert await wire.delete_session() == 204
        assert _session(wire) is None
        assert await wire.delete_session() is None  # never deleted twice
        await wire.close()


async def test_fake_without_sessions_and_old_protocol(tmp_path: Path) -> None:
    transport = McpTransportModel(
        response_mode="json",
        issues_session=False,
        requires_session=False,
        protocol_versions={"2024-11-05": "2024-11-05"},
    )
    async with streaming_client(fake_app(tmp_path, fake_model(transport=transport))) as http:
        wire = McpWireClient("http://fake.test/mcp", http, protocol_version="2024-11-05")
        await wire.initialize()
        assert wire.session_id is None
        assert wire.negotiated_version == "2024-11-05"
        listed = await wire.call("tools/list")
        headers = {k.lower() for k, _ in listed.request_headers}
        assert protocol.PROTOCOL_HEADER not in headers  # 2024-11-05 predates the header
        assert protocol.SESSION_HEADER not in headers
        assert (await wire.get_stream_status())[0] == 405
        assert await wire.delete_session() is None
        await wire.close()


async def test_batch_post(tmp_path: Path) -> None:
    async with streaming_client(fake_app(tmp_path)) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        batch: JsonValue = [protocol.request(10, "ping"), protocol.request(11, "tools/list")]
        call = await wire.post(batch)
        assert call.method == "batch"
        assert [j(m)["id"] for m in call.messages] == [10, 11]
        assert call.response is None  # a batch has no single reply
        await wire.close()


async def test_fake_legacy_sse_transport(tmp_path: Path) -> None:
    transport = McpTransportModel(kind="sse", path="/sse", messages_path="/rpc/messages/")
    async with streaming_client(fake_app(tmp_path, fake_model(transport=transport))) as http:
        wire = McpWireClient("http://fake.test/sse", http, transport="sse", timeout_s=5)
        init = await wire.initialize()
        assert wire.messages_url is not None
        assert wire.messages_url.startswith("http://fake.test/rpc/messages/?session_id=")
        assert wire.sse_content_type is not None
        assert "event-stream" in wire.sse_content_type
        assert init.status == 202
        assert j(init.result)["serverInfo"]["name"] == "Demo"
        assert [event[1] for event in init.sse_events] == ["message"]
        assert init.sse_events[0][0] >= 0
        echo = await wire.call("tools/call", {"name": "echo", "arguments": {"text": "sse"}})
        assert j(echo.result)["content"][0]["text"] == "sse"
        assert echo.body == "Accepted"
        note = await wire.notify("notifications/progress")
        assert note.response is None
        assert note.sse_events == []
        assert await wire.delete_session() is None  # sessions end with the stream
        await wire.close()
        await wire.close()  # idempotent


# -- against the official MCP SDK server -----------------------------------------------------------


@pytest.mark.parametrize("json_response", [False, True])
async def test_official_sdk_server(json_response: bool) -> None:
    async with sdk_client(json_response=json_response) as http:
        wire = McpWireClient("http://sdk.test/mcp", http)
        init = await wire.initialize()
        assert wire.session_id
        assert wire.negotiated_version == protocol.LATEST_HANDSHAKE_VERSION
        info = j(init.result)
        assert info["serverInfo"]["name"] == "sdk-demo"
        assert set(info["capabilities"]) >= {"tools", "resources", "prompts"}
        streamed = not json_response
        assert ("event-stream" in (init.content_type or "")) is streamed
        tools, _ = await wire.list_all("tools/list", "tools")
        by_name = {t["name"]: t for t in j(tools)}
        assert by_name["echo"]["annotations"]["readOnlyHint"] is True
        assert "annotations" not in by_name["delete_item"]
        echo = await wire.call("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        assert j(echo.result)["structuredContent"] == {"result": "hi"}
        unknown = await wire.call("tools/call", {"name": "nope", "arguments": {}})
        assert j(unknown.result)["isError"] is True  # the SDK answers with a tool error
        missing = await wire.call("fauxcall/unknown")
        assert j(missing.error)["code"] == protocol.METHOD_NOT_FOUND
        no_session = await wire.post(protocol.request(5, "tools/list"), include_session=False)
        assert no_session.status == 400
        assert j(no_session.error)["message"] == "Bad Request: Missing session ID"
        status, content_type = await wire.get_stream_status()
        assert (status, content_type) == (200, "text/event-stream")
        assert await wire.delete_session() == 200
        gone = await McpWireClient("http://sdk.test/mcp", http).call("tools/list")
        assert gone.status == 400
        await wire.close()


@pytest.mark.parametrize(
    ("offered", "answered"),
    [
        ("2024-11-05", "2024-11-05"),
        ("2025-03-26", "2025-03-26"),
        ("2025-11-25", "2025-11-25"),
        ("1999-01-01", "2025-11-25"),
    ],
)
async def test_official_sdk_version_negotiation(offered: str, answered: str) -> None:
    async with sdk_client(json_response=True) as http:
        wire = McpWireClient("http://sdk.test/mcp", http, protocol_version=offered)
        await wire.initialize()
        assert wire.negotiated_version == answered
        await wire.close()


# -- scripted servers for the edge cases -----------------------------------------------------------


class Feed(httpx2.AsyncByteStream):
    """A response body the test writes to as it goes (``None`` ends it)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False

    def push(self, text: str) -> None:
        self.queue.put_nowait(text.encode())

    def end(self) -> None:
        self.queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (chunk := await self.queue.get()) is not None:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _client(handler: Callable[[httpx2.Request], Any]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), base_url="http://mock")


def _sse(*messages: JsonValue) -> str:
    return "".join(protocol.sse_event(m) for m in messages)


async def test_sse_reply_stops_reading_an_open_stream() -> None:
    feed = Feed()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        sent = jsonfmt.loads(request.content)
        feed.push(_sse(protocol.notification("notifications/message", {"level": "info"})))
        feed.push("event: ping\ndata: {}\n\ndata: not json\n\n")
        feed.push(_sse(protocol.result(j(sent)["id"], {"tools": []}))[:-1])
        feed.push("\n")  # the reply completes in a later chunk; the stream then stays open
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=feed)

    async with _client(handler) as http:
        wire = McpWireClient("http://mock/mcp", http)
        call = await asyncio.wait_for(wire.call("tools/list"), timeout=2)
    assert call.result == {"tools": []}
    assert [event[1] for event in call.sse_events] == ["message", "ping", "message", "message"]
    assert len(call.messages) == 2  # the ping and the unparseable event carry no JSON-RPC
    assert feed.closed


async def test_sse_reply_without_final_blank_line() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        reply = protocol.result(j(jsonfmt.loads(request.content))["id"], {"ok": 1})
        body = f"data: {jsonfmt.dumps(reply)}"
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with _client(handler) as http:
        call = await McpWireClient("http://mock/mcp", http).call("ping")
    assert call.result == {"ok": 1}
    assert len(call.sse_events) == 1


@pytest.mark.parametrize(
    ("status", "content_type", "body", "expected"),
    [
        (200, "application/json", "<html>oops</html>", None),
        (200, "application/json", "{not json", None),
        (500, "text/plain", "Internal Server Error", None),
        (
            400,
            "application/json",
            '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Bad"}}',
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Bad"}},
        ),
        (
            200,
            "application/json",
            '{"jsonrpc":"2.0","id":999,"result":{}}',
            None,  # a reply to someone else
        ),
    ],
)
async def test_lenient_replies(
    status: int, content_type: str, body: str, expected: JsonValue
) -> None:
    async with _client(
        lambda _: httpx2.Response(status, headers={"content-type": content_type}, text=body)
    ) as http:
        call = await McpWireClient("http://mock/mcp", http).call("tools/list")
    assert call.response == expected
    assert call.body == body


async def test_initialize_failures() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        sent = j(jsonfmt.loads(request.content))
        return httpx2.Response(200, json=protocol.error(sent["id"], -32602, "Unsupported"))

    async with _client(refuse) as http:
        with pytest.raises(RecordingError, match="MCP initialize failed: HTTP 200"):
            await McpWireClient("http://mock/mcp", http).initialize()
    async with _client(lambda _: httpx2.Response(405)) as http:
        with pytest.raises(RecordingError, match="HTTP 405"):
            await McpWireClient("http://mock/mcp", http).initialize()


async def test_list_all_stops_on_repeated_cursor_and_errors() -> None:
    def looping(request: httpx2.Request) -> httpx2.Response:
        sent = j(jsonfmt.loads(request.content))
        return httpx2.Response(
            200, json=protocol.result(sent["id"], {"tools": [{"name": "t"}], "nextCursor": "same"})
        )

    async with _client(looping) as http:
        items, calls = await McpWireClient("http://mock/mcp", http).list_all("tools/list", "tools")
    assert len(calls) == 2  # the cursor repeated: stop instead of looping forever
    assert items == [{"name": "t"}, {"name": "t"}]

    def failing(request: httpx2.Request) -> httpx2.Response:
        sent = j(jsonfmt.loads(request.content))
        return httpx2.Response(200, json=protocol.error(sent["id"], -32601, "Method not found"))

    async with _client(failing) as http:
        items, calls = await McpWireClient("http://mock/mcp", http).list_all(
            "prompts/list", "prompts"
        )
    assert (items, len(calls)) == ([], 1)

    def odd(request: httpx2.Request) -> httpx2.Response:
        sent = j(jsonfmt.loads(request.content))
        return httpx2.Response(200, json=protocol.result(sent["id"], {"tools": "not a list"}))

    async with _client(odd) as http:
        items, _ = await McpWireClient("http://mock/mcp", http).list_all("tools/list", "tools")
    assert items == []


# -- the legacy HTTP+SSE transport, scripted -------------------------------------------------------


class LegacyServer:
    """A scripted legacy server: ``GET /sse`` streams :attr:`feed`, ``POST /messages`` replies."""

    def __init__(self, *, get_status: int = 200, post_status: int = 202) -> None:
        self.feed = Feed()
        self.get_status = get_status
        self.post_status = post_status
        self.on_post: Callable[[JsonValue], None] = self.reply

    def reply(self, message: JsonValue) -> None:
        if protocol.is_request(message):
            self.feed.push(_sse(protocol.result(j(message)["id"], {"echo": j(message)["method"]})))

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            if self.get_status != 200:
                return httpx2.Response(self.get_status, text="nope")
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=self.feed
            )
        self.on_post(jsonfmt.loads(request.content))
        return httpx2.Response(self.post_status, text="Accepted")


@asynccontextmanager
async def _legacy(server: LegacyServer, *, timeout_s: float = 2.0) -> AsyncIterator[McpWireClient]:
    async with _client(server) as http:
        wire = McpWireClient("http://mock/sse", http, transport="sse", timeout_s=timeout_s)
        try:
            yield wire
        finally:
            await wire.close()


async def test_legacy_transport_skips_noise_until_the_reply() -> None:
    server = LegacyServer()
    server.feed.push("event: endpoint\ndata: /messages?session_id=abc\n\n")

    def noisy(message: JsonValue) -> None:
        if protocol.is_request(message):
            server.feed.push("event: heartbeat\ndata: 1\n\n")
            server.feed.push("data: not json\n\n")
            server.feed.push(_sse(protocol.notification("notifications/message")))
            server.feed.push(_sse(protocol.result(12345, {"other": True})))
        server.reply(message)

    server.on_post = noisy
    async with _legacy(server) as wire:
        init = await wire.initialize()
        assert wire.messages_url == "http://mock/messages?session_id=abc"
        names = [event[1] for event in init.sse_events]
        assert names == ["heartbeat", "message", "message", "message", "message"]
        assert len(init.messages) == 3  # a notification, a stray reply and ours
        assert j(init.result) == {"echo": "initialize"}
        assert wire.negotiated_version is None  # this server doesn't report a version


async def test_legacy_stream_must_open_with_an_endpoint() -> None:
    async with _legacy(LegacyServer(get_status=500)) as wire:
        with pytest.raises(RecordingError, match="returned 500"):
            await wire.initialize()
        assert wire.messages_url is None
    server = LegacyServer()
    server.feed.push(_sse(protocol.notification("hello")))
    async with _legacy(server) as wire:
        with pytest.raises(RecordingError, match="expected an 'endpoint' event, got 'message'"):
            await wire.initialize()
    server = LegacyServer()
    server.feed.end()
    async with _legacy(server) as wire:
        with pytest.raises(RecordingError, match="the end of the stream"):
            await wire.initialize()


async def test_legacy_stream_ending_or_silence() -> None:
    server = LegacyServer()
    server.feed.push("event: endpoint\ndata: /messages\n\n")
    async with _legacy(server, timeout_s=0.2) as wire:
        await wire.initialize()
        server.on_post = lambda _message: None
        with pytest.raises(RecordingError, match=r"no reply within 0.2s"):
            await wire.call("tools/list")
        server.feed.end()
        with pytest.raises(RecordingError, match="ended before the reply"):
            await wire.call("tools/list")
        with pytest.raises(RecordingError, match="the event stream is closed"):
            await wire.call("tools/list")


async def test_legacy_post_errors_and_order() -> None:
    server = LegacyServer(post_status=400)
    server.feed.push("event: endpoint\ndata: /messages\n\n")
    async with _legacy(server) as wire:
        with pytest.raises(RecordingError, match="call initialize\\(\\) first"):
            await wire.call("ping")
        with pytest.raises(RecordingError, match="MCP initialize failed: HTTP 400"):
            await wire.initialize()


async def test_list_all_gives_up_after_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fauxcall.mcpwire.client.MAX_PAGES", 3)
    counter = iter(range(100))

    def endless(request: httpx2.Request) -> httpx2.Response:
        sent = j(jsonfmt.loads(request.content))
        page: JsonValue = {"tools": [{"name": "t"}], "nextCursor": f"c{next(counter)}"}
        return httpx2.Response(200, json=protocol.result(sent["id"], page))

    async with _client(endless) as http:
        items, calls = await McpWireClient("http://mock/mcp", http).list_all("tools/list", "tools")
    assert (len(items), len(calls)) == (3, 3)
