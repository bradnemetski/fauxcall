from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx2
import pytest
from httpx2.websockets import ASGIWebSocketTransport, WebSocketDisconnect, WebSocketUpgradeError

from _engine import API_KEY, OPEN_NS, harness, market_model
from fauxcall.mcpwire.protocol import parse_sse
from fauxcall.model.fields import NowPolicy
from fauxcall.timefmt import TimeFormat, parse_iso

if TYPE_CHECKING:
    from pathlib import Path


async def test_sse_replays_recorded_events_with_live_times(tmp_path: Path) -> None:
    model = market_model()
    endpoint = model.endpoint("GET /v1/stream/trades")
    endpoint.fields = [
        NowPolicy(path="time", scope="body", format=TimeFormat(kind="iso", fraction_digits=3))
    ]
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        response = await client.get("/v1/stream/trades", params={"symbol": "AAPL"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [(name, event_id) for name, _, event_id in events] == [("trade", "1"), ("trade", "2")]
    first = json.loads(events[0][1])
    assert first["price"] == "227.51"
    assert parse_iso(first["time"]) == OPEN_NS  # timestamp is "now" at emission, not 2026-10-09
    # The recorded 200 ms spacing is replayed (scaled by speed=1000).
    assert any(abs(sleep - 0.0002) < 1e-9 for sleep in fake.sleeps)


async def test_sse_without_recordings_is_the_real_404(tmp_path: Path) -> None:
    model = market_model()
    model.endpoint("GET /v1/stream/trades").stream.exchanges = ["999999"]  # type: ignore[union-attr]
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        assert (await client.get("/v1/stream/trades")).status_code == 404


async def test_websocket_conversation_replays_matching_replies(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    transport = ASGIWebSocketTransport(app=fake.app)
    async with (
        httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client,
        client.websocket("/v1/ws/quotes", headers={"X-API-Key": API_KEY}) as ws,
    ):
        await ws.send_text(json.dumps({"symbols": ["AAPL"], "action": "subscribe"}))
        assert json.loads(await ws.receive_text()) == {
            "type": "subscribed",
            "symbols": ["AAPL"],
        }
        quote = json.loads(await ws.receive_text())
        assert quote["type"] == "quote"
        assert quote["price"] == "227.50"


async def test_websocket_auth_failure_rejects_the_handshake(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    transport = ASGIWebSocketTransport(app=fake.app)
    with pytest.raises(
        (WebSocketUpgradeError, WebSocketDisconnect, httpx2.HTTPError, ExceptionGroup)
    ):
        async with (
            httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client,
            client.websocket("/v1/ws/quotes") as ws,
        ):
            await ws.receive_text()
