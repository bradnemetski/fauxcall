"""Streams: the SSE trade walk (compared call by call) and the WebSocket quote feed."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from feeds import TRANSCRIPTS, shape, talk

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient, FakeUnderTest


MICROS_Z = re.compile(r"T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
MILLIS_Z = re.compile(r"T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _events(text: str) -> list[dict[str, Any]]:
    events = []
    for frame in filter(None, text.split("\n\n")):
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        fields["data"] = json.loads(fields["data"])
        events.append(fields)
    return events


@pytest.mark.parametrize(
    ("symbol", "count"), [("msft", 4), ("9984.T", 2), ("bp.l", 1), ("HSBA.L", None)]
)
async def test_trade_stream(api: ApiClient, symbol: str, count: int | None) -> None:
    params = {"symbol": symbol} | ({"count": str(count)} if count else {})
    response = await api.get("/v1/stream/trades", params=params)
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert [event["id"] for event in events] == [str(n) for n in range(1, (count or 10) + 1)]
    assert {event["event"] for event in events} == {"trade"}
    quote = (await api.get(f"/v1/tickers/{symbol}/quote")).json()
    for event in events:
        trade = event["data"]
        assert trade["symbol"] == symbol.upper()
        assert abs(float(trade["price"]) - float(quote["price"])) < float(quote["price"]) * 0.05
        assert 0 < trade["size"] <= 1000
        assert MILLIS_Z.search(trade["time"])
        stamp = datetime.fromisoformat(trade["time"])
        assert abs((datetime.now(UTC) - stamp).total_seconds()) < 30
    # the walk itself (prices, sizes), compared with the real API's (parity compares shapes only)
    walk = [(e["data"]["price"], e["data"]["size"]) for e in events]
    key = f"walk:{symbol}:{count}"
    if api.is_real:
        TRANSCRIPTS[key] = walk
    elif key in TRANSCRIPTS:
        assert walk == TRANSCRIPTS[key]


async def test_trade_walk_is_deterministic(api: ApiClient) -> None:
    longer = _events((await api.get("/v1/stream/trades?symbol=CAT&count=3")).text)
    shorter = _events((await api.get("/v1/stream/trades?symbol=cat&count=2")).text)
    strip = [{k: v for k, v in e["data"].items() if k != "time"} for e in longer]
    assert strip[:2] == [{k: v for k, v in e["data"].items() if k != "time"} for e in shorter]


CONVERSATIONS: dict[str, tuple[list[str], int]] = {
    "subscribe": (['{"action":"subscribe","symbols":["jpm","JPM","0700.hk"]}'], 12),
    "ping-then-subscribe": (
        ['{"action":"ping"}', '{"action":"subscribe","symbols":["SAN.PA"]}'],
        8,
    ),
    "errors": (
        ["oops", "[1]", '{"action":"jump"}', '{"action":null}', '{"action":"subscribe"}'],
        5,
    ),
    "unknown-symbol": (['{"action":"subscribe","symbols":["KO","pepsi"]}'], 1),
}


@pytest.mark.parametrize("name", sorted(CONVERSATIONS))
async def test_quote_feed(api: ApiClient, fauxcall_fake: FakeUnderTest, name: str) -> None:
    sends, expect = CONVERSATIONS[name]
    conversation = await talk(fauxcall_fake, real=api.is_real, sends=sends, expect=expect)
    if name in {"subscribe", "ping-then-subscribe"}:
        assert conversation.messages[-1] == {"type": "done"}
        assert conversation.closed == (1000, "")
        quotes = [m for m in conversation.messages if m["type"] == "quote"]
        assert len(quotes) % 5 == 0
        assert all(MICROS_Z.search(m["as_of"]) for m in quotes)
    else:
        assert {m["type"] for m in conversation.messages} == {"error"}
        assert conversation.closed is None  # errors keep the socket open
    key = f"feed:{name}"
    if api.is_real:
        TRANSCRIPTS[key] = shape(conversation)
    elif key in TRANSCRIPTS:
        assert shape(conversation) == TRANSCRIPTS[key]


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        (None, "Missing api_key query parameter."),
        ("nobody", "Invalid API key."),
        ("revoked-key", "This API key has been revoked."),
    ],
)
async def test_quote_feed_refuses_bad_keys(
    api: ApiClient, fauxcall_fake: FakeUnderTest, key: str | None, reason: str
) -> None:
    conversation = await talk(fauxcall_fake, real=api.is_real, sends=[], expect=0, key=key)
    assert conversation.closed == (4401, reason)
    assert conversation.messages == []
