"""``WS /v1/ws/quotes`` in-process: auth on connect, the subscribe protocol, errors and timing.

Each test opens the fake in its own task (the WebSocket transport's task group must be entered and
exited by the same task) and catches close frames inside the session.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from httpx2.websockets import ASGIWebSocketTransport, WebSocketDisconnect

from fauxcall_refapi import handlers
from fauxcall_refapi.app import build_app
from fauxcall_refapi.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from httpx2.websockets import AsyncWebSocketSession

PATH = "/v1/ws/quotes"
DEMO = {"api_key": "demo-key"}
RECORDING_DAY = "2026-10-11T10:37:46Z"  # Sunday: every market closed
CLOSED_AAPL = {
    "type": "quote",
    "symbol": "AAPL",
    "price": "227.48",
    "as_of": "2026-10-09T20:00:00.000000Z",
}


@contextlib.asynccontextmanager
async def _fake(at: str = RECORDING_DAY) -> AsyncIterator[httpx2.AsyncClient]:
    settings = Settings(fast=True, log_level="WARNING")
    async with (
        ASGIWebSocketTransport(app=build_app(settings)) as transport,
        httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client,
    ):
        response = await client.post("/__fauxcall/clock", json={"op": "freeze", "at": at})
        assert response.status_code == 200
        yield client


async def _recv(ws: AsyncWebSocketSession) -> Any:
    return json.loads(await ws.receive_text(timeout=5))


async def _until_closed(ws: AsyncWebSocketSession) -> tuple[list[Any], WebSocketDisconnect]:
    messages: list[Any] = []
    while True:
        try:
            messages.append(await _recv(ws))
        except WebSocketDisconnect as closed:
            return messages, closed


async def test_subscription_streams_five_rounds_then_closes() -> None:
    async with _fake() as client, client.websocket(PATH, params=DEMO) as ws:
        await ws.send_json({"action": "subscribe", "symbols": ["msft", "AAPL", "7203.t", "MSFT"]})
        messages, closed = await _until_closed(ws)
    assert messages[0] == {"type": "subscribed", "symbols": ["MSFT", "AAPL", "7203.T"]}
    quotes = messages[1:-1]
    assert len(quotes) == 15  # 5 rounds x 3 symbols, in subscription order (000813)
    assert [q["symbol"] for q in quotes[:3]] == ["MSFT", "AAPL", "7203.T"]
    assert quotes[1] == CLOSED_AAPL
    assert quotes[2]["as_of"] == "2026-10-09T06:30:00.000000Z"
    assert messages[-1] == {"type": "done"}
    assert closed.code == 1000


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("hello", "Invalid JSON."),
        ('["AAPL"]', "Messages must be JSON objects."),
        ('"subscribe"', "Messages must be JSON objects."),
        ('{"symbols": ["AAPL"]}', "Unknown action: null."),
        ('{"action": "Subscribe"}', 'Unknown action: "Subscribe".'),
        ('{"action": 5}', "Unknown action: 5."),
        ('{"action": {"a": 1, "b": [1, 2]}}', 'Unknown action: {"a":1,"b":[1,2]}.'),
        ('{"action": "subscribe"}', handlers.WS_BAD_SYMBOLS),
        ('{"action": "subscribe", "symbols": []}', handlers.WS_BAD_SYMBOLS),
        ('{"action": "subscribe", "symbols": "AAPL"}', handlers.WS_BAD_SYMBOLS),
        ('{"action": "subscribe", "symbols": ["AAPL", 1]}', handlers.WS_BAD_SYMBOLS),
        ('{"action": "subscribe", "symbols": ["ZZZ", "AAPL", "NOPE"]}', "Unknown symbol 'ZZZ'."),
        ('{"action": "subscribe", "symbols": [" AAPL "]}', "Unknown symbol ' AAPL '."),
        ('{"action": "subscribe", "symbols": [""]}', "Unknown symbol ''."),
    ],
)
async def test_errors_keep_the_socket_open(message: str, error: str) -> None:
    async with _fake() as client, client.websocket(PATH, params={"api_key": "readonly-key"}) as ws:
        await ws.send_text(message)
        assert await _recv(ws) == {"type": "error", "message": error}
        await ws.send_json({"action": "subscribe", "symbols": ["AAPL"]})
        messages, closed = await _until_closed(ws)
    assert messages[:2] == [{"type": "subscribed", "symbols": ["AAPL"]}, CLOSED_AAPL]
    assert closed.code == 1000


async def test_binary_frames_earn_an_error() -> None:
    async with _fake() as client, client.websocket(PATH, params=DEMO) as ws:
        await ws.send_bytes(b'{"action":"subscribe","symbols":["AAPL"]}')
        assert await _recv(ws) == {"type": "error", "message": handlers.WS_BINARY}
        await ws.send_json({"action": "subscribe", "symbols": ["AAPL"]})
        messages, closed = await _until_closed(ws)
    assert messages[:2] == [{"type": "subscribed", "symbols": ["AAPL"]}, CLOSED_AAPL]
    assert closed.code == 1000


async def test_messages_while_streaming_are_ignored() -> None:
    async with _fake() as client, client.websocket(PATH, params=DEMO) as ws:
        await ws.send_json({"action": "subscribe", "symbols": ["AAPL"]})
        await ws.send_json({"action": "subscribe", "symbols": ["MSFT"]})
        messages, closed = await _until_closed(ws)
    assert {m.get("symbol") for m in messages[1:-1]} == {"AAPL"}
    assert len(messages) == 7
    assert closed.code == 1000


@pytest.mark.parametrize(
    ("params", "headers", "reason"),
    [
        ({}, {}, "Missing api_key query parameter."),
        ({"api_key": ""}, {}, "Missing api_key query parameter."),
        ({}, {"X-API-Key": "demo-key"}, "Missing api_key query parameter."),  # 000832
        ({"api_key": "wrong"}, {}, "Invalid API key."),
        ({"api_key": "DEMO-KEY"}, {}, "Invalid API key."),
        ({"api_key": "revoked-key"}, {}, "This API key has been revoked."),
    ],
)
async def test_bad_credentials_close_4401(
    params: dict[str, str], headers: dict[str, str], reason: str
) -> None:
    async with _fake() as client, client.websocket(PATH, params=params, headers=headers) as ws:
        messages, closed = await _until_closed(ws)
    assert messages == []
    assert (closed.code, closed.reason) == (4401, reason)


async def test_open_market_quotes_follow_the_rest_quote() -> None:
    async with _fake("2026-10-12T14:00:00Z") as client:  # Monday 10:00 New York
        async with client.websocket(PATH, params=DEMO) as ws:
            await ws.send_json({"action": "subscribe", "symbols": ["AAPL"]})
            messages, _ = await _until_closed(ws)
        rest = await client.get("/v1/tickers/AAPL/quote", headers={"X-API-Key": "demo-key"})
    quote = messages[1]
    assert quote["as_of"] == "2026-10-12T13:59:58.500000Z"
    assert (rest.json()["price"], rest.json()["as_of"]) == (quote["price"], quote["as_of"])


async def test_client_disconnect_is_quiet() -> None:
    async with _fake() as client, client.websocket(PATH, params=DEMO):
        pass  # the handler sees the disconnect while waiting for a message
