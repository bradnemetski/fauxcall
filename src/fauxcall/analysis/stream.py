"""What an MCP server sends on its own: the messages recorded on the client's GET stream.

Introspection's watch of the stream and plans that ``listen`` record them (``MCP GET:…``
exchanges). The kinds become the model's ``server_messages`` (parity expects a scenario to
compare each), and the first one's members give ``message_member_order``. Where the server gave up
on a request the client left unanswered (plans' ``client_replies`` with ``null``), the reason of
its ``notifications/cancelled`` gives ``timeout_cancel_reason``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fauxcall import jsonfmt
from fauxcall.model.recording import UNANSWERED

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from fauxcall.model.recording import Exchange
    from fauxcall.types import JsonObject

STREAM_PREFIX = "MCP GET"


@dataclass
class StreamReport:
    """The messages servers sent on GET streams, by method."""

    counts: Counter[str] = field(default_factory=Counter)
    requests: set[str] = field(default_factory=set)
    """Methods sent as requests (they carry an id): the client must answer them."""
    member_order: list[str] = field(default_factory=list)
    """The members of the first message, in the order the server wrote them."""
    timeout_reasons: Counter[str] = field(default_factory=Counter)
    """Reasons the server gave when it cancelled its own request the client never answered."""

    @property
    def timeout_reason(self) -> str | None:
        """The reason the server gives most often (``None``: no timeout recorded)."""
        common = self.timeout_reasons.most_common(1)
        return common[0][0] if common else None

    @property
    def methods(self) -> list[str]:
        """Every method seen, most frequent first."""
        return [method for method, _ in self.counts.most_common()]


def stream_report(exchanges: Iterable[Exchange]) -> StreamReport:
    """What the server sent on the client's GET stream, across the recordings."""
    report = StreamReport()
    for exchange in exchanges:
        on_stream = exchange.endpoint.startswith(STREAM_PREFIX)
        if not on_stream and UNANSWERED not in exchange.tags:
            continue
        asked: set[str] = set()  # ids of the server's requests to the client
        for message in _messages(exchange):
            method = str(message["method"])
            if "id" in message:
                asked.add(jsonfmt.dumps(message["id"]))
            elif method == "notifications/cancelled":
                _note_timeout(report, message, asked)
            if not on_stream:
                continue
            report.counts[method] += 1
            if "id" in message:
                report.requests.add(method)
            if not report.member_order:
                report.member_order = list(message)
    return report


def _messages(exchange: Exchange) -> Iterator[JsonObject]:
    """The JSON-RPC messages with a method (requests, notifications) on an exchange's stream."""
    for event in exchange.stream:
        try:
            message = jsonfmt.loads(event.data or "")
        except ValueError:
            continue  # a priming event, a comment
        if isinstance(message, dict) and isinstance(message.get("method"), str):
            yield message


def _note_timeout(report: StreamReport, message: JsonObject, asked: set[str]) -> None:
    """Count the reason of a cancellation of one of the server's own requests."""
    params = message.get("params")
    if not isinstance(params, dict) or "requestId" not in params:
        return
    reason = params.get("reason")
    if jsonfmt.dumps(params["requestId"]) in asked and isinstance(reason, str):
        report.timeout_reasons[reason] += 1
