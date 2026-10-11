from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from starlette.websockets import WebSocketDisconnect

from fauxcall_reference_api.simulation import format_price, trade_walk

from .helpers import DEMO, at, error_of

if TYPE_CHECKING:
    from starlette.testclient import TestClient

    from fauxcall_reference_api.clock import ManualClock


BAD_COUNT = "Invalid value for parameter 'count': must be an integer between 1 and 50."


def parse_sse(text: str) -> list[dict[str, str]]:
    events = []
    for block in text.split("\n\n"):
        if block:
            fields = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append(fields)
    return events


def test_trade_stream_framing_and_timing(client: TestClient, clock: ManualClock) -> None:
    response = client.get("/v1/stream/trades?symbol=aapl&count=3", headers=DEMO)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.text.endswith("\n\n")
    walk = trade_walk("AAPL", Decimal("227.48"), 3)
    expected = [
        {
            "id": str(n),
            "event": "trade",
            "data": json.dumps(
                {
                    "symbol": "AAPL",
                    "price": format_price(walk[n - 1].price),
                    "size": walk[n - 1].size,
                    "time": time,
                },
                separators=(",", ":"),
            ),
        }
        for n, time in [
            (1, "2026-10-09T15:00:00.000Z"),
            (2, "2026-10-09T15:00:00.200Z"),
            (3, "2026-10-09T15:00:00.400Z"),
        ]
    ]
    assert parse_sse(response.text) == expected
    assert clock.sleeps == [0.2, 0.2]


def test_trade_stream_is_identical_on_every_request(client: TestClient, clock: ManualClock) -> None:
    first = client.get("/v1/stream/trades?symbol=7203.T&count=50", headers=DEMO)
    clock.set_ns(at(2026, 10, 12, 1, 0))
    second = client.get("/v1/stream/trades?symbol=7203.t&count=50", headers=DEMO)
    strip = [
        {k: v for k, v in json.loads(e["data"]).items() if k != "time"}
        for e in parse_sse(first.text)
    ]
    again = [
        {k: v for k, v in json.loads(e["data"]).items() if k != "time"}
        for e in parse_sse(second.text)
    ]
    assert strip == again
    assert len(strip) == 50
    assert [e["id"] for e in parse_sse(second.text)] == [str(n) for n in range(1, 51)]


def test_trade_stream_defaults_to_ten_events(client: TestClient) -> None:
    response = client.get("/v1/stream/trades?symbol=MSFT", headers=DEMO)
    assert len(parse_sse(response.text)) == 10


@pytest.mark.parametrize(
    ("query", "code", "param", "message"),
    [
        ("symbol=NOPE", "unknown_symbol", "symbol", "Unknown symbol 'NOPE'."),
        ("", "missing_parameter", "symbol", "Missing required parameter 'symbol'."),
        ("symbol=AAPL&count=0", "invalid_parameter", "count", BAD_COUNT),
        ("symbol=AAPL&count=51", "invalid_parameter", "count", BAD_COUNT),
    ],
)
def test_trade_stream_errors(
    client: TestClient, query: str, code: str, param: str, message: str
) -> None:
    response = client.get(f"/v1/stream/trades?{query}", headers=DEMO)
    assert response.status_code == 400
    assert error_of(response.json()) == {
        "code": code,
        "message": message,
        "request_id": response.json()["error"]["request_id"],
        "param": param,
    }


def test_websocket_quote_feed(client: TestClient, clock: ManualClock) -> None:
    clock.set_ns(at(2026, 10, 9, 11, 0, tz="America/New_York"))  # NYSE open, Tokyo closed
    with client.websocket_connect("/v1/ws/quotes?api_key=readonly-key") as ws:
        ws.send_json({"action": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_json({"action": "subscribe", "symbols": ["aapl", "7203.T", "AAPL"]})
        assert ws.receive_json() == {"type": "subscribed", "symbols": ["AAPL", "7203.T"]}
        messages = [ws.receive_json() for _ in range(10)]
        assert ws.receive_json() == {"type": "done"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1000
    as_of = [
        "2026-10-09T14:59:58.500000Z",
        "2026-10-09T14:59:58.750000Z",
        "2026-10-09T14:59:59.000000Z",
        "2026-10-09T14:59:59.250000Z",
        "2026-10-09T14:59:59.500000Z",
    ]
    expected = []
    for round_as_of in as_of:
        expected.append(
            {"type": "quote", "symbol": "AAPL", "price": "227.48", "as_of": round_as_of}
        )
        expected.append(
            {
                "type": "quote",
                "symbol": "7203.T",
                "price": "2741.50",
                "as_of": "2026-10-09T06:30:00.000000Z",
            }
        )
    assert messages == expected
    assert clock.sleeps == [0.25, 0.25, 0.25, 0.25]


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("not json", "Invalid JSON."),
        ("[1]", "Messages must be JSON objects."),
        ('{"action": "dance"}', 'Unknown action: "dance".'),
        ("{}", "Unknown action: null."),
        ('{"action": "subscribe"}', "symbols must be a non-empty array of strings."),
        ('{"action": "subscribe", "symbols": []}', "symbols must be a non-empty array of strings."),
        (
            '{"action": "subscribe", "symbols": "AAPL"}',
            "symbols must be a non-empty array of strings.",
        ),
        (
            '{"action": "subscribe", "symbols": ["AAPL", 1]}',
            "symbols must be a non-empty array of strings.",
        ),
        ('{"action": "subscribe", "symbols": ["AAPL", "nope"]}', "Unknown symbol 'nope'."),
    ],
)
def test_websocket_protocol_errors_keep_the_socket_open(
    client: TestClient, message: str, error: str
) -> None:
    with client.websocket_connect("/v1/ws/quotes?api_key=demo-key") as ws:
        ws.send_text(message)
        assert ws.receive_json() == {"type": "error", "message": error}
        ws.send_json({"action": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_websocket_rejects_binary_frames(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws/quotes?api_key=demo-key") as ws:
        ws.send_bytes(b'{"action": "ping"}')
        assert ws.receive_json() == {
            "type": "error",
            "message": "Binary messages are not supported.",
        }


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("", "Missing api_key query parameter."),
        ("?api_key=", "Missing api_key query parameter."),
        ("?api_key=nope", "Invalid API key."),
        ("?api_key=revoked-key", "This API key has been revoked."),
    ],
)
def test_websocket_auth_failures_close_with_4401(
    client: TestClient, query: str, reason: str
) -> None:
    with (
        client.websocket_connect(f"/v1/ws/quotes{query}") as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_json()
    assert closed.value.code == 4401
    assert closed.value.reason == reason


def test_websocket_header_key_is_not_enough(client: TestClient) -> None:
    with (
        client.websocket_connect("/v1/ws/quotes", headers=DEMO) as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_json()
    assert closed.value.code == 4401


def test_websocket_ignores_messages_during_the_stream(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws/quotes?api_key=demo-key") as ws:
        ws.send_json({"action": "subscribe", "symbols": ["AAPL"]})
        ws.send_json({"action": "ping"})  # arrives while streaming: never answered
        received = [ws.receive_json() for _ in range(7)]
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert [m["type"] for m in received] == ["subscribed", *["quote"] * 5, "done"]
    assert closed.value.code == 1000


def test_websocket_client_may_leave_early(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws/quotes?api_key=demo-key") as ws:
        ws.send_json({"action": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.close()
