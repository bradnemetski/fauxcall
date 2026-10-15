"""SSE answers as the SDK's Streamable HTTP transport writes them.

Every message is an event ``event: message``, ``id: <event id>``, ``data: <JSON>``, in that order
(``writeSSEEvent``); a message the server sends of its own accord has its members in the SDK's
order, ``{method, params, jsonrpc, id}`` (000218, 000346, 000411). A POST's answer opens with an
empty priming event only when the protocol version the client asked for sorts at or after
2025-11-25 (``writePrimingEvent``): the handshake's own version (000509 primes for 2099-01-01,
000510 not for 1999-01-01), else the request's ``MCP-Protocol-Version`` header, 2025-03-26 without
one. A GET stream never gets one.

FauxCall's runtime writes ``id:`` first and ``jsonrpc`` first, and primes by the negotiated
version: :class:`SseRewriter` turns its events into the SDK's as they stream.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Final

from fauxcall import jsonfmt

from fauxcall_everything.surrogates import reveal

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue

PRIMING_SINCE: Final = "2025-11-25"
DEFAULT_NEGOTIATED: Final = "2025-03-26"
"""The SDK's ``DEFAULT_NEGOTIATED_PROTOCOL_VERSION``: a request without the header."""
SDK_ORDER: Final[tuple[str, ...]] = ("method", "params", "jsonrpc", "id")
_FIELDS: Final = (b"event:", b"id:", b"data:")


def primes(version: str) -> bool:
    """Whether a POST answer opens with a priming event (a plain string comparison)."""
    return version >= PRIMING_SINCE


def priming_event() -> bytes:
    """An empty event a client may resume from."""
    return f"id: {uuid.uuid4()}\ndata: \n\n".encode()


def message_event(message: JsonValue) -> bytes:
    """One message as the SDK writes it."""
    return f"event: message\nid: {uuid.uuid4()}\ndata: {jsonfmt.dumps(message)}\n\n".encode()


class SseRewriter:
    """The runtime's SSE body as the SDK writes it, rewritten event by event as it streams.

    Args:
        prime: Whether the stream opens with a priming event (the runtime's is kept, or one is
            added; ``False`` drops the runtime's).
    """

    def __init__(self, *, prime: bool) -> None:
        self.prime = prime
        self.pending = b""
        self.opened = False

    def feed(self, chunk: bytes, *, last: bool) -> bytes:
        """The complete events so far, rewritten (a partial event waits for the rest)."""
        data = self.pending + chunk
        end = data.rfind(b"\n\n")
        cut = len(data) if last else end + 2 if end >= 0 else 0
        complete, self.pending = data[:cut], data[cut:]
        *events, tail = complete.split(b"\n\n")
        written = b"".join(self._event(event) for event in events)
        return reveal(written + (self._opening() if tail and not self.opened else b"") + tail)

    def _opening(self) -> bytes:
        """What goes before the stream's first event: a priming event, if it primes."""
        self.opened = True
        return priming_event() if self.prime else b""

    def _event(self, event: bytes) -> bytes:
        lines = event.split(b"\n")
        if _is_priming(lines):
            if self.opened:  # only a stream's first event primes
                return b""
            self.opened = True
            return event + b"\n\n" if self.prime else b""
        opening = b"" if self.opened else self._opening()
        if not any(line.startswith(_FIELDS) for line in lines):  # a keep-alive comment
            return opening + event + b"\n\n"
        ordered = sorted(lines, key=_field_rank)
        return opening + b"\n".join(_reordered(line) for line in ordered) + b"\n\n"


def _is_priming(lines: list[bytes]) -> bool:
    """An event with an id and empty data only."""
    fields = {line.split(b":", 1)[0] for line in lines if line}
    return fields == {b"id", b"data"} and all(
        line.rstrip() == b"data:" for line in lines if line.startswith(b"data:")
    )


def _field_rank(line: bytes) -> int:
    for rank, field in enumerate(_FIELDS):
        if line.startswith(field):
            return rank
    return len(_FIELDS)


def _reordered(line: bytes) -> bytes:
    """A ``data:`` line carrying a request or notification, with its members in the SDK's order.

    The spelling is FauxCall's own, read and written back: the line keeps its length.
    """
    if not line.startswith(b"data: "):
        return line
    try:
        message = jsonfmt.loads(line[len(b"data: ") :])
    except ValueError:
        return line
    if not isinstance(message, dict) or "method" not in message:
        return line
    ordered: JsonObject = {name: message[name] for name in SDK_ORDER if name in message}
    ordered.update(message)
    written = b"data: " + jsonfmt.dumps(ordered).encode()
    return written if len(written) == len(line) else line
