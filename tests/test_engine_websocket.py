"""WebSocket replay: greetings on connect, matched replies with recorded timing, push streams."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from httpx2.websockets import ASGIWebSocketTransport, AsyncWebSocketSession, WebSocketDisconnect

from _engine import API_KEY, RECORDED_AT, harness, market_model
from fauxcall.clock import datetime_to_ns
from fauxcall.model import CapturedRequest, CapturedResponse, EndpointModel, Exchange, StreamEvent
from fauxcall.recording.store import RecordingStore
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from _engine import Harness

FEED = "GET /v1/ws/feed"


def _fake(tmp_path: Path, events: list[StreamEvent] | None, **stream: Any) -> Harness:
    model = market_model()
    model.upsert_endpoint(
        EndpointModel.model_validate(
            {
                "id": FEED,
                "method": "GET",
                "path": "/v1/ws/feed",
                "role": "websocket",
                "stream": {"kind": "websocket", **stream},
            }
        )
    )
    fake = harness(tmp_path, model=model)
    if events is not None:
        store = RecordingStore(tmp_path / "data" / "recordings")
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint=FEED,
                started_at=RECORDED_AT,
                started_ns=datetime_to_ns(RECORDED_AT),
                request=CapturedRequest(method=HttpMethod.GET, url="x", path="/v1/ws/feed"),
                response=CapturedResponse(status=101),
                stream=events,
            )
        )
    return fake


def _client(fake: Harness, *, key: str | None = API_KEY) -> httpx2.AsyncClient:
    headers = {"X-API-Key": key} if key else {}
    transport = ASGIWebSocketTransport(app=fake.app)
    return httpx2.AsyncClient(transport=transport, base_url="http://fake.test", headers=headers)


def _close_code(error: BaseException) -> int | None:
    """The close code of a disconnect, possibly wrapped in the transport's exception group."""
    if isinstance(error, WebSocketDisconnect):
        return error.code
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            code = _close_code(inner)
            if code is not None:
                return code
    return None


def _send(t_ms: float, data: str) -> StreamEvent:
    return StreamEvent(t_ms=t_ms, kind="ws_send", data=data)


def _recv(t_ms: float, data: str) -> StreamEvent:
    return StreamEvent(t_ms=t_ms, kind="ws_recv", data=data)


CONVERSATION = [
    _recv(50, '{"type":"welcome"}'),
    _send(100, '{"action":"subscribe","symbol":"AAPL"}'),
    _recv(350, '{"type":"subscribed","symbol":"AAPL"}'),
    _recv(400, '{"type":"quote","price":"227.50"}'),
    _send(900, "ping"),
    _recv(905, "pong"),
    _send(1000, '{"action":"bye"}'),
    _recv(1010, '{"type":"bye"}'),
    StreamEvent(t_ms=1020, kind="ws_close", code=4000),
]


async def _talk(fake: Harness, script: Callable[[AsyncWebSocketSession], Awaitable[None]]) -> int:
    """Run ``script`` on a connection, then wait for the server to close; returns the close code."""
    try:
        async with _client(fake) as client, client.websocket("/v1/ws/feed") as ws:
            await script(ws)
            await ws.receive_text()
    except (WebSocketDisconnect, ExceptionGroup) as error:
        code = _close_code(error)
        if code is None:  # something else failed inside the transport's task group
            raise
        return code
    raise AssertionError("the server never closed the connection")


async def test_greeting_matched_replies_timing_and_recorded_close(tmp_path: Path) -> None:
    fake = _fake(tmp_path, CONVERSATION)
    received: list[str] = []

    async def script(ws: AsyncWebSocketSession) -> None:
        received.append(await ws.receive_text())  # sent before the client says anything
        # Key order and spacing differ from the recording: still the same JSON message.
        await ws.send_text('{ "symbol": "AAPL", "action": "subscribe" }')
        received.extend([await ws.receive_text(), await ws.receive_text()])
        await ws.send_text("nobody recorded this")  # ignored, like an unknown command
        await ws.send_text("ping")  # plain text matches exactly
        received.append(await ws.receive_text())
        await ws.send_text('{"action":"bye"}')
        received.append(await ws.receive_text())

    assert await _talk(fake, script) == 4000
    assert received == [
        '{"type":"welcome"}',
        '{"type":"subscribed","symbol":"AAPL"}',
        '{"type":"quote","price":"227.50"}',
        "pong",
        '{"type":"bye"}',
    ]
    # 50 ms greeting delay, then replies paced from the client's message: 250 ms, then 50 ms.
    assert fake.sleeps[:3] == pytest.approx([0.05, 0.25, 0.05])


async def test_push_stream_replays_server_messages_and_closes(tmp_path: Path) -> None:
    events = [_recv(0, "a"), _send(5, "ignored"), _recv(200, "b")]
    fake = _fake(tmp_path, events, respond_to="none", speed=2)
    received: list[str] = []

    async def script(ws: AsyncWebSocketSession) -> None:
        received.extend([await ws.receive_text(), await ws.receive_text()])

    assert await _talk(fake, script) == 1000
    assert received == ["a", "b"]
    assert fake.sleeps == pytest.approx([0.1])  # 200 ms at speed 2


async def test_looped_push_stream_repeats_with_a_pause(tmp_path: Path) -> None:
    fake = _fake(tmp_path, [_recv(0, "tick"), _recv(0, "tock")], respond_to="none", loop=True)
    async with _client(fake) as client, client.websocket("/v1/ws/feed") as ws:
        received = [await ws.receive_text() for _ in range(5)]
    assert received == ["tick", "tock", "tick", "tock", "tick"]
    assert fake.sleeps[:2] == pytest.approx([1.0, 1.0])  # zero-gap recordings repeat once a second


async def test_echo_mode(tmp_path: Path) -> None:
    fake = _fake(tmp_path, [_recv(0, "x")], respond_to="echo")
    async with _client(fake) as client, client.websocket("/v1/ws/feed") as ws:
        await ws.send_text("hello")
        assert await ws.receive_text() == "hello"
        await ws.send_bytes(b"\x00raw")
        assert await ws.receive_bytes() == b"\x00raw"  # binary comes back as binary


async def test_binary_client_frames_match_the_recorded_bytes(tmp_path: Path) -> None:
    fake = _fake(tmp_path, None)
    store = RecordingStore(tmp_path / "data" / "recordings")
    store.save(
        Exchange(
            id=store.next_id(),
            endpoint=FEED,
            started_at=RECORDED_AT,
            started_ns=datetime_to_ns(RECORDED_AT),
            request=CapturedRequest(method=HttpMethod.GET, url="x", path="/v1/ws/feed"),
            response=CapturedResponse(status=101),
            stream=[
                StreamEvent(t_ms=0, kind="ws_send", binary_file="sent.bin"),
                _recv(5, '{"type":"error","message":"Binary messages are not supported."}'),
                _send(10, '{"action":"ping"}'),
                _recv(12, '{"type":"pong"}'),
            ],
        ),
        sidecars={"sent.bin": b"\x01\x02"},
    )
    received: list[str] = []
    async with _client(fake) as client, client.websocket("/v1/ws/feed") as ws:
        await ws.send_bytes(b"\x09")  # bytes nobody recorded: ignored, like an unknown command
        await ws.send_text("\u0001\u0002")  # the same bytes as text: a different message
        await ws.send_bytes(b"\x01\x02")
        received.append(await ws.receive_text())
        await ws.send_text('{"action":"ping"}')
        received.append(await ws.receive_text())
    assert received == [
        '{"type":"error","message":"Binary messages are not supported."}',
        '{"type":"pong"}',
    ]


async def test_no_recordings_closes_with_1011(tmp_path: Path) -> None:
    fake = _fake(tmp_path, None)
    with pytest.raises((WebSocketDisconnect, ExceptionGroup)) as closed:
        async with _client(fake) as client, client.websocket("/v1/ws/feed") as ws:
            await ws.receive_text()
    assert _close_code(closed.value) == 1011


@pytest.mark.parametrize(("close_code", "expected"), [(4401, 4401), (None, 1008)])
async def test_auth_failures(tmp_path: Path, close_code: int | None, expected: int | None) -> None:
    fake = _fake(tmp_path, CONVERSATION, auth_failure_close_code=close_code)
    with pytest.raises((WebSocketDisconnect, ExceptionGroup, httpx2.HTTPError)) as failed:
        async with _client(fake, key=None) as client, client.websocket("/v1/ws/feed") as ws:
            await ws.receive_text()
    # A recorded close code means "accept, then close"; otherwise the handshake is refused (a real
    # server answers 403; the in-process transport reports that as close code 1008).
    assert _close_code(failed.value) == expected


async def test_recordings_that_close_the_socket(tmp_path: Path) -> None:
    rejected = [StreamEvent(t_ms=5, kind="ws_close", code=4003)]  # closed before any message
    assert await _talk(_fake(tmp_path / "a", rejected), _idle) == 4003
    pushed = [_recv(0, "only"), StreamEvent(t_ms=0, kind="ws_close", code=4001)]
    fake = _fake(tmp_path / "b", pushed, respond_to="none", loop=True)
    received: list[str] = []

    async def script(ws: AsyncWebSocketSession) -> None:
        received.append(await ws.receive_text())

    assert await _talk(fake, script) == 4001  # a recorded close ends even a looped stream
    assert received == ["only"]


async def test_binary_or_empty_server_messages_are_sent_as_empty_text(tmp_path: Path) -> None:
    fake = _fake(tmp_path, [StreamEvent(t_ms=0, kind="ws_recv", data=None)], respond_to="none")
    received: list[str] = []

    async def script(ws: AsyncWebSocketSession) -> None:
        received.append(await ws.receive_text())

    assert await _talk(fake, script) == 1000
    assert received == [""]


async def _idle(ws: AsyncWebSocketSession) -> None:
    del ws


async def test_binary_frames_and_close_reasons_replay_as_recorded(tmp_path: Path) -> None:
    fake = _fake(tmp_path, None, respond_to="none")
    store = RecordingStore(tmp_path / "data" / "recordings")
    store.save(
        Exchange(
            id=store.next_id(),
            endpoint=FEED,
            started_at=RECORDED_AT,
            started_ns=datetime_to_ns(RECORDED_AT),
            request=CapturedRequest(method=HttpMethod.GET, url="x", path="/v1/ws/feed"),
            response=CapturedResponse(status=101),
            stream=[
                StreamEvent(t_ms=0, kind="ws_recv", binary_file="frame-1.bin"),
                StreamEvent(t_ms=1, kind="ws_recv", binary_file="lost.bin"),  # sidecar missing
                StreamEvent(t_ms=2, kind="ws_close", code=4002, data="bye now"),
            ],
        ),
        sidecars={"frame-1.bin": b"\x00\x01binary"},
    )
    received: list[bytes] = []

    async def script(ws: AsyncWebSocketSession) -> None:
        received.append(await ws.receive_bytes())

    disconnect: list[WebSocketDisconnect] = []
    try:
        async with _client(fake) as client, client.websocket("/v1/ws/feed") as ws:
            await script(ws)
            await ws.receive_text()
    except (WebSocketDisconnect, ExceptionGroup) as error:
        disconnect.extend(_disconnects(error))
    assert received == [b"\x00\x01binary"]
    assert [(d.code, d.reason) for d in disconnect] == [(4002, "bye now")]


def _disconnects(error: BaseException) -> list[WebSocketDisconnect]:
    if isinstance(error, WebSocketDisconnect):
        return [error]
    if isinstance(error, BaseExceptionGroup):
        return [d for inner in error.exceptions for d in _disconnects(inner)]
    return []
