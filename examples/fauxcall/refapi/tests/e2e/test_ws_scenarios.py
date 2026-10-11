"""End-to-end scenarios for ``WS /v1/ws/quotes`` (the parity client speaks HTTP only).

Each scenario opens the WebSocket on the side under test itself: the real server over the network,
or the fake in-process. The assertions are exact (the protocol is deterministic) and hold on both
sides; each scenario also reads the REST quote through ``api``, so parity compares that call and
the WebSocket values are checked against it.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx2
import pytest
from fauxcall.model.fields import RandomPolicy
from fauxcall.testing import CompareOptions
from httpx2.websockets import ASGIWebSocketTransport, WebSocketDisconnect
from market import exchanges_by_code, get_quote

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fauxcall.testing import ApiClient, FakeUnderTest
    from httpx2.websockets import AsyncWebSocketSession

PATH: Final = "/v1/ws/quotes"
DEMO: Final = {"api_key": "demo-key"}
UNROUTED: Final = CompareOptions(
    policies=[RandomPolicy(kind="random", scope="body", path="error.request_id", generator="uuid4")]
)
"""Unrouted requests map to no endpoint: give their error bodies the request_id policy."""
BAD_SYMBOLS: Final = "symbols must be a non-empty array of strings."


@contextlib.asynccontextmanager
async def ws_client(fake: FakeUnderTest, target: str) -> AsyncIterator[httpx2.AsyncClient]:
    """An HTTP client that can open WebSockets on the side under test."""
    if target == "real":
        async with httpx2.AsyncClient(base_url=fake.real_base_url or "") as client:
            yield client
        return
    async with (
        ASGIWebSocketTransport(app=fake.app_factory()) as transport,
        httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client,
    ):
        yield client


async def until_closed(
    ws: AsyncWebSocketSession,
) -> tuple[list[tuple[float, Any]], WebSocketDisconnect]:
    """Every message (with its arrival time) until the server closes the socket."""
    messages: list[tuple[float, Any]] = []
    while True:
        try:
            text = await ws.receive_text(timeout=10)
        except WebSocketDisconnect as closed:
            return messages, closed
        messages.append((time.monotonic(), json.loads(text)))


def instant(text: str) -> float:
    return datetime.fromisoformat(text).astimezone(UTC).timestamp()


async def test_ws_subscription_matches_rest_quotes(
    api: ApiClient, fauxcall_fake: FakeUnderTest, fauxcall_target: str
) -> None:
    symbols = ["msft", "AAPL", "7203.t", "0700.HK", "MSFT"]
    async with (
        ws_client(fauxcall_fake, fauxcall_target) as client,
        client.websocket(PATH, params=DEMO) as ws,
    ):
        await ws.send_json({"action": "subscribe", "symbols": symbols})
        timed, closed = await until_closed(ws)
    messages = [m for _, m in timed]
    expected = ["MSFT", "AAPL", "7203.T", "0700.HK"]
    assert messages[0] == {"type": "subscribed", "symbols": expected}
    assert messages[-1] == {"type": "done"}
    assert closed.code == 1000
    quotes = messages[1:-1]
    assert [q["symbol"] for q in quotes] == expected * 5
    exchanges = await exchanges_by_code(api)
    for symbol in expected:
        ticker = (await api.get(f"/v1/tickers/{symbol}")).json()
        rest = await get_quote(api, symbol, exchanges[ticker["exchange"]])
        mine = [q for q in quotes if q["symbol"] == symbol]
        assert all(set(q) == {"type", "symbol", "price", "as_of"} for q in mine)
        assert {q["price"] for q in mine} == {rest["price"]}  # prices are static (documented)
        if rest["market_state"] == "closed":
            assert {q["as_of"] for q in mine} == {rest["as_of"]}
        else:  # documented: as_of = now - 1.5 s while the session is open
            for quote in mine:
                assert abs(time.time() - 1.5 - instant(quote["as_of"])) < 10
    if fauxcall_target == "real":  # the fake's fast mode skips the 250 ms pauses by design
        round_starts = [t for t, m in timed if m.get("symbol") == "MSFT"]
        gaps = [b - a for a, b in itertools.pairwise(round_starts)]
        assert all(0.2 < gap < 0.4 for gap in gaps), gaps


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("hello", "Invalid JSON."),
        ("[1]", "Messages must be JSON objects."),
        ("null", "Messages must be JSON objects."),
        ('{"action": "unsubscribe", "symbols": ["AAPL"]}', 'Unknown action: "unsubscribe".'),
        ('{"action": "SUBSCRIBE"}', 'Unknown action: "SUBSCRIBE".'),
        ('{"action": true}', "Unknown action: true."),
        ('{"action": {"a": 1, "b": ["x", "y"]}}', 'Unknown action: {"a":1,"b":["x","y"]}.'),
        ('{"action": "subscribe", "symbols": {}}', BAD_SYMBOLS),
        ('{"action": "subscribe", "symbols": [null]}', BAD_SYMBOLS),
        ('{"action": "subscribe", "symbols": ["aapl", "nvdx", "zz"]}', "Unknown symbol 'nvdx'."),
        ('{"action": "subscribe", "symbols": ["O\'NEIL"]}', "Unknown symbol 'O'NEIL'."),
    ],
)
async def test_ws_errors_then_recover(
    api: ApiClient,
    fauxcall_fake: FakeUnderTest,
    fauxcall_target: str,
    message: str,
    error: str,
) -> None:
    async with (
        ws_client(fauxcall_fake, fauxcall_target) as client,
        client.websocket(PATH, params={"api_key": "readonly-key"}) as ws,
    ):
        await ws.send_text(message)
        assert json.loads(await ws.receive_text(timeout=10)) == {"type": "error", "message": error}
        await ws.send_json({"action": "subscribe", "symbols": ["nvda"]})
        timed, closed = await until_closed(ws)
    messages = [m for _, m in timed]
    assert messages[0] == {"type": "subscribed", "symbols": ["NVDA"]}
    assert len(messages) == 7
    assert closed.code == 1000
    ticker = (await api.get("/v1/tickers/NVDA")).json()  # no time fields: see market.get_quote
    assert {q["symbol"] for q in messages[1:-1]} == {ticker["symbol"]}


@pytest.mark.parametrize(
    ("params", "headers", "reason"),
    [
        ({}, {}, "Missing api_key query parameter."),
        ({"api_key": ""}, {}, "Missing api_key query parameter."),
        ({}, {"X-API-Key": "demo-key"}, "Missing api_key query parameter."),
        ({"api_key": "nope"}, {}, "Invalid API key."),
        ({"api_key": "demo-key "}, {}, "Invalid API key."),
        ({"api_key": "revoked-key"}, {}, "This API key has been revoked."),
    ],
)
async def test_ws_auth_failures(  # noqa: PLR0913, PLR0917 - fixtures + parameters
    api: ApiClient,
    fauxcall_fake: FakeUnderTest,
    fauxcall_target: str,
    params: dict[str, str],
    headers: dict[str, str],
    reason: str,
) -> None:
    async with (
        ws_client(fauxcall_fake, fauxcall_target) as client,
        client.websocket(PATH, params=params, headers=headers) as ws,
    ):
        timed, closed = await until_closed(ws)
    assert timed == []
    assert (closed.code, closed.reason) == (4401, reason)
    assert (await api.get("/v1/time", headers={"X-API-Key": "nope"})).status_code == 401


async def test_ws_binary_frames_then_recover(
    api: ApiClient, fauxcall_fake: FakeUnderTest, fauxcall_target: str
) -> None:
    async with (
        ws_client(fauxcall_fake, fauxcall_target) as client,
        client.websocket(PATH, params=DEMO) as ws,
    ):
        for frame in (b'{"action":"subscribe","symbols":["AAPL"]}', b"\x00\xff"):
            await ws.send_bytes(frame)
            reply = json.loads(await ws.receive_text(timeout=10))
            assert reply == {"type": "error", "message": "Binary messages are not supported."}
        await ws.send_json({"action": "subscribe", "symbols": ["AAPL"]})
        timed, closed = await until_closed(ws)
    assert [m["type"] for _, m in timed] == ["subscribed", *["quote"] * 5, "done"]
    assert closed.code == 1000
    plain = await api.get("/v1/ws/quotes", compare=UNROUTED)  # plain HTTP: no such route
    assert (plain.status_code, plain.json()["error"]["code"]) == (404, "not_found")


async def test_ws_does_not_count_against_the_rate_limit(
    api: ApiClient, fauxcall_fake: FakeUnderTest, fauxcall_target: str
) -> None:
    before = await api.get("/v1/time", headers={"X-API-Key": "readonly-key"})
    async with (
        ws_client(fauxcall_fake, fauxcall_target) as client,
        client.websocket(PATH, params={"api_key": "readonly-key"}) as ws,
    ):
        await ws.send_json({"action": "subscribe", "symbols": ["AAPL"]})
        await until_closed(ws)
    after = await api.get("/v1/time", headers={"X-API-Key": "readonly-key"})
    remaining = [r.headers.get("x-ratelimit-remaining") for r in (before, after)]
    if None not in remaining:  # the fake's fast mode (parity) does not rate limit
        assert int(remaining[0] or 0) - int(remaining[1] or 0) in {1, -29}  # same or new window
