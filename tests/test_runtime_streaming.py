from __future__ import annotations

from typing import TYPE_CHECKING

from _engine import RECORDED_AT
from fauxcall.clock import datetime_to_ns
from fauxcall.model import CapturedRequest, CapturedResponse, Exchange, StreamEvent
from fauxcall.runtime.streaming import _same_message, pick_stream_exchange, sse_frame
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from fauxcall.types import JsonValue


def test_sse_frames() -> None:
    full = StreamEvent(t_ms=0, kind="sse", id="7", event="trade", retry=3000)
    assert sse_frame(full, "a\nb") == "id: 7\nevent: trade\nretry: 3000\ndata: a\ndata: b\n\n"
    plain = StreamEvent(t_ms=0, kind="sse", event="message")  # the default event name
    assert sse_frame(plain, None) == "data: \n\n"


def test_message_matching() -> None:
    assert _same_message('{"b": 1, "a": [1.0]}', '{"a":[1],"b":1}')  # JSON by value
    assert not _same_message('{"a": 1}', '{"a": 2}')
    assert _same_message("ping", "ping")
    assert not _same_message("ping", "pong")
    assert not _same_message("ping", None)  # a recorded binary message never matches text


def _stream(query: list[tuple[str, str]]) -> Exchange:
    return Exchange(
        id="-".join(v for _, v in query) or "none",
        endpoint="GET /s",
        started_at=RECORDED_AT,
        started_ns=datetime_to_ns(RECORDED_AT),
        request=CapturedRequest(method=HttpMethod.GET, url="x", path="/s", query=query),
        response=CapturedResponse(status=200),
    )


def test_stream_selection_prefers_the_closest_query() -> None:
    streams = [
        _stream([]),
        _stream([("symbol", "AAPL")]),
        _stream([("symbol", "MSFT"), ("n", "5")]),
    ]
    assert pick_stream_exchange([], {}) is None
    chosen = pick_stream_exchange(streams, {"symbol": "MSFT", "n": "5"})
    assert chosen is not None
    assert chosen.id == "MSFT-5"
    first: JsonValue = pick_stream_exchange(streams, {"other": "x"}).id  # type: ignore[union-attr]
    assert first == "none"  # no overlap anywhere: the first recording
