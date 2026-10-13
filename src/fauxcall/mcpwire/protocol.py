"""MCP's JSON-RPC 2.0 framing, shared by the recorder and the fake server.

FauxCall speaks MCP at the wire level on purpose: a fake must return the real server's JSON
byte-for-byte (unknown fields included), which a high-level SDK would normalize away.

Server-Sent Events are parsed by :class:`SseDecoder` following the WHATWG event-stream rules:
CRLF, LF and a lone CR all end a line, a leading byte-order mark is ignored, comments (``:``) and
unknown fields are skipped, one space after the colon is removed, ``event:`` with an empty value
means ``message``, ``retry:`` only accepts digits, and a block without ``data`` dispatches nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fauxcall import jsonfmt

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
REQUEST_TIMEOUT: Final = -32001
"""The MCP SDKs' code for a request the other side never answered."""

LATEST_HANDSHAKE_VERSION: Final = "2025-06-18"
KNOWN_VERSIONS: Final = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

SESSION_HEADER: Final = "mcp-session-id"
PROTOCOL_HEADER: Final = "mcp-protocol-version"

_LINE_END: Final = re.compile(r"\r\n|\r|\n")
_BOM: Final = "﻿"


def request(request_id: int | str, method: str, params: JsonValue = None) -> JsonObject:
    """A JSON-RPC request object."""
    message: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: JsonValue = None) -> JsonObject:
    """A JSON-RPC notification (no id, no response)."""
    message: JsonObject = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def result(request_id: JsonValue, value: JsonValue) -> JsonObject:
    """A successful JSON-RPC response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def error(request_id: JsonValue, code: int, message: str, data: JsonValue = None) -> JsonObject:
    """A JSON-RPC error response."""
    body: JsonObject = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


def with_id(message: JsonValue, request_id: JsonValue) -> JsonObject:
    """A recorded response re-addressed to a new request id (key order preserved)."""
    if not isinstance(message, dict):
        raise TypeError("a JSON-RPC response must be an object")
    return {key: (request_id if key == "id" else value) for key, value in message.items()}


def is_request(message: JsonValue) -> bool:
    """A request has a method and an id."""
    return isinstance(message, dict) and "method" in message and "id" in message


def is_notification(message: JsonValue) -> bool:
    """A notification has a method and no id."""
    return isinstance(message, dict) and "method" in message and "id" not in message


def is_response(message: JsonValue) -> bool:
    """A response has an id and a result or an error."""
    return (
        isinstance(message, dict)
        and "id" in message
        and ("result" in message or "error" in message)
    )


# ---------------------------------------------------------------------------
# Server-Sent Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One dispatched Server-Sent Event."""

    event: str = "message"
    data: str = ""
    id: str | None = None
    retry: int | None = None


class SseDecoder:
    """Incremental Server-Sent Events parser: feed text as it arrives, get complete events.

    ``id`` and ``retry`` are reported on the event whose block carried them; a block with only
    ``id``/``retry`` (no ``data``) passes them on to the next event, so nothing is lost.
    """

    __slots__ = ("_buffer", "_data", "_event", "_id", "_retry", "_started")

    def __init__(self) -> None:
        self._buffer = ""
        self._started = False
        self._event: str | None = None
        self._data: list[str] = []
        self._id: str | None = None
        self._retry: int | None = None

    def feed(self, text: str) -> list[SseEvent]:
        """Add a chunk of the stream; returns the events it completed."""
        if not self._started and text:
            self._started = True
            text = text.removeprefix(_BOM)
        self._buffer += text
        events: list[SseEvent] = []
        position = 0
        for match in _LINE_END.finditer(self._buffer):
            if match.group() == "\r" and match.end() == len(self._buffer):
                break  # maybe the first half of "\r\n": wait for the next chunk
            event = self._line(self._buffer[position : match.start()])
            position = match.end()
            if event is not None:
                events.append(event)
        self._buffer = self._buffer[position:]
        return events

    def close(self, *, dispatch_incomplete: bool = False) -> list[SseEvent]:
        """End of stream: returns any events still completed by it.

        An event without its terminating blank line is dropped (as browsers do) unless
        ``dispatch_incomplete`` is set.
        """
        events: list[SseEvent] = []
        if self._buffer.endswith("\r"):
            events += self.feed("\n")
        if self._buffer:
            self._line(self._buffer)  # an unterminated line is never blank, so it never dispatches
            self._buffer = ""
        if dispatch_incomplete:
            event = self._dispatch()
            if event is not None:
                events.append(event)
        self._event, self._data, self._id, self._retry = None, [], None, None
        return events

    def _line(self, line: str) -> SseEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, colon, value = line.partition(":")
        if colon:
            value = value.removeprefix(" ")
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id" and "\0" not in value:
            self._id = value
        elif name == "retry" and value.isascii() and value.isdigit():
            self._retry = int(value)
        return None

    def _dispatch(self) -> SseEvent | None:
        if not self._data:
            self._event = None
            return None
        event = SseEvent(
            event=self._event or "message",
            data="\n".join(self._data),
            id=self._id,
            retry=self._retry,
        )
        self._event, self._data, self._id, self._retry = None, [], None, None
        return event


def parse_sse_events(text: str) -> list[SseEvent]:
    """Every event of a complete SSE body (a final event missing its blank line is kept)."""
    decoder = SseDecoder()
    return [*decoder.feed(text), *decoder.close(dispatch_incomplete=True)]


def parse_sse(text: str) -> list[tuple[str, str, str | None]]:
    """Split an SSE body into ``(event, data, id)`` triples.

    Multi-line data is joined with newlines; a final event missing its blank line is kept.
    """
    return [(event.event, event.data, event.id) for event in parse_sse_events(text)]


def messages_from_body(body: str, content_type: str | None) -> list[JsonValue]:
    """JSON-RPC messages carried by an HTTP body (JSON or SSE).

    SSE events that are not ``message`` events or don't hold JSON are skipped; a JSON body that
    does not parse raises :class:`ValueError`.
    """
    if not body.strip():
        return []
    if content_type and "text/event-stream" in content_type.lower():
        found: list[JsonValue] = []
        for event, data, _ in parse_sse(body):
            if event == "message" and data.strip():
                try:
                    found.append(jsonfmt.loads(data))
                except ValueError:
                    continue
        return found
    parsed = jsonfmt.loads(body)
    return list(parsed) if isinstance(parsed, list) else [parsed]


def sse_event(message: JsonValue, *, event_id: str | None = None) -> str:
    """One SSE ``message`` event carrying a JSON-RPC message."""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("event: message")
    lines.append(f"data: {jsonfmt.dumps(message)}")
    return "\n".join(lines) + "\n\n"
