from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fauxcall import jsonfmt
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.protocol import SseDecoder, SseEvent, parse_sse, parse_sse_events

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue


def test_message_builders() -> None:
    assert protocol.request(1, "tools/list") == {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert protocol.request("a", "x", {"k": 1})["params"] == {"k": 1}
    assert protocol.notification("notifications/initialized") == {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    assert protocol.notification("n", [1])["params"] == [1]
    assert protocol.result(3, {"ok": True}) == {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
    assert protocol.error(None, -32601, "Method not found") == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32601, "message": "Method not found"},
    }
    assert protocol.error(4, -32602, "bad", {"field": "x"})["error"] == {
        "code": -32602,
        "message": "bad",
        "data": {"field": "x"},
    }


def test_with_id_keeps_key_order_and_unknown_members() -> None:
    recorded: JsonObject = {"result": {"a": 1}, "jsonrpc": "2.0", "x-vendor": True, "id": 7}
    readdressed = protocol.with_id(recorded, "abc")
    assert list(readdressed) == ["result", "jsonrpc", "x-vendor", "id"]
    assert readdressed["id"] == "abc"
    assert recorded["id"] == 7  # the recording is not modified
    assert protocol.with_id({"jsonrpc": "2.0", "result": {}}, 1) == {"jsonrpc": "2.0", "result": {}}
    with pytest.raises(TypeError):
        protocol.with_id([1, 2], 1)


def test_message_kinds() -> None:
    request = protocol.request(1, "ping")
    note = protocol.notification("notifications/progress")
    reply = protocol.result(1, {})
    failure = protocol.error(None, -32700, "Parse error")
    assert protocol.is_request(request)
    assert not protocol.is_request(note)
    assert protocol.is_notification(note)
    assert not protocol.is_notification(request)
    assert protocol.is_response(reply)
    assert protocol.is_response(failure)
    assert not protocol.is_response(request)
    others: list[JsonValue] = [None, "x", [request]]
    for other in others:
        assert not protocol.is_request(other)
        assert not protocol.is_notification(other)
        assert not protocol.is_response(other)


def test_parse_sse_basics() -> None:
    body = 'event: message\nid: 1\ndata: {"a":1}\n\n: keep-alive\n\ndata: plain\n\n'
    assert parse_sse(body) == [("message", '{"a":1}', "1"), ("message", "plain", None)]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # CRLF, LF and a lone CR all end lines
        (
            "data: a\r\n\r\ndata: b\n\ndata: c\r\r",
            [("message", "a", None), ("message", "b", None), ("message", "c", None)],
        ),
        # multi-line data is joined with newlines; a bare "data" line adds an empty line
        ("data: one\ndata: two\ndata\n\n", [("message", "one\ntwo\n", None)]),
        # only one space after the colon is removed; no colon means an empty value
        ("data:  spaced\n\n", [("message", " spaced", None)]),
        # custom event names; an empty event name means "message"
        (
            "event: endpoint\ndata: /messages/\n\nevent:\ndata: x\n\n",
            [("endpoint", "/messages/", None), ("message", "x", None)],
        ),
        # a leading byte-order mark is ignored
        ("﻿event: ping\ndata: 1\n\n", [("ping", "1", None)]),
        # comments and unknown fields are skipped
        (": hello\nfoo: bar\ndata: y\n\n", [("message", "y", None)]),
        # a block without data dispatches nothing (its event name is dropped)
        ("event: lonely\n\ndata: z\n\n", [("message", "z", None)]),
        # an id containing NUL is ignored
        ("id: a\u0000b\ndata: q\n\n", [("message", "q", None)]),
        # an id-only block passes its id on to the next event
        ("id: 41\n\ndata: carried\n\n", [("message", "carried", "41")]),
        # a final event without its blank line is kept by parse_sse
        ("data: a\n\ndata: tail", [("message", "a", None), ("message", "tail", None)]),
        ("", []),
        ("\n\n\n", []),
    ],
)
def test_parse_sse_edge_cases(body: str, expected: list[tuple[str, str, str | None]]) -> None:
    assert parse_sse(body) == expected


def test_retry_is_reported_on_the_next_event() -> None:
    events = parse_sse_events("retry: 3000\n\ndata: a\n\nretry: soon\ndata: b\nretry: 15\n\n")
    assert events == [SseEvent(data="a", retry=3000), SseEvent(data="b", retry=15)]


def test_decoder_is_incremental() -> None:
    decoder = SseDecoder()
    assert decoder.feed("da") == []
    assert decoder.feed("ta: x\r") == []  # the CR might be the first half of CRLF
    assert decoder.feed("\n\r") == []
    assert decoder.feed("\nevent: e\n") == [SseEvent(data="x")]
    assert decoder.feed("data: y\n\n") == [SseEvent(event="e", data="y")]
    assert decoder.feed("") == []
    assert decoder.close() == []


def test_decoder_close() -> None:
    decoder = SseDecoder()
    decoder.feed("data: dropped")
    assert decoder.close() == []  # browsers drop an event cut off by the end of the stream
    decoder = SseDecoder()
    decoder.feed("data: kept\n")
    assert decoder.close(dispatch_incomplete=True) == [SseEvent(data="kept")]
    decoder = SseDecoder()
    decoder.feed("data: cr\n\r")  # a trailing CR is a complete (blank) line at the end
    assert decoder.close() == [SseEvent(data="cr")]
    assert decoder.close(dispatch_incomplete=True) == []  # nothing pending afterwards


def test_bom_only_stripped_at_the_start() -> None:
    decoder = SseDecoder()
    assert decoder.feed("﻿data: a\n\n") == [SseEvent(data="a")]
    assert decoder.feed("﻿data: b\n\n") == []  # a BOM later on is just a strange field name


def test_messages_from_body() -> None:
    assert protocol.messages_from_body("  ", "application/json") == []
    assert protocol.messages_from_body('{"jsonrpc":"2.0","id":1,"result":{}}', None) == [
        {"jsonrpc": "2.0", "id": 1, "result": {}}
    ]
    batch = '[{"jsonrpc":"2.0","id":1,"result":1},{"jsonrpc":"2.0","id":2,"result":2}]'
    assert len(protocol.messages_from_body(batch, "application/json")) == 2
    sse = (
        "event: endpoint\ndata: /x\n\n"
        "data: not json\n\n"
        "data:   \n\n"
        'data: {"jsonrpc":"2.0","id":3,"result":{"price":105.50}}\n\n'
    )
    (message,) = protocol.messages_from_body(sse, "Text/Event-Stream; charset=utf-8")
    assert jsonfmt.dumps(message) == '{"jsonrpc":"2.0","id":3,"result":{"price":105.50}}'
    with pytest.raises(ValueError, match="Expecting value"):
        protocol.messages_from_body("<html>", "application/json")


def test_sse_event_round_trip() -> None:
    message = protocol.result(9, {"text": "line1\nline2", "n": jsonfmt.loads("1.50")})
    framed = protocol.sse_event(message)
    assert framed.startswith("event: message\ndata: ")
    assert framed.endswith("\n\n")
    assert protocol.messages_from_body(framed, "text/event-stream") == [message]
    with_id = protocol.sse_event(message, event_id="e1")
    assert with_id.startswith("id: e1\nevent: message\n")
    assert parse_sse(with_id)[0][2] == "e1"
    assert jsonfmt.dumps(protocol.messages_from_body(framed, "text/event-stream")[0]).endswith(
        '"n":1.50}}'
    )
