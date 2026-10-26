"""What an MCP server sends on its own: the messages recorded on the client's GET stream.

Introspection's watch of the stream and plans that ``listen`` record them (``MCP GET:…``
exchanges). The kinds become the model's ``server_messages`` (parity expects a scenario to
compare each), and the first one's members give ``message_member_order``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fauxcall import jsonfmt

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fauxcall.model.recording import Exchange

STREAM_PREFIX = "MCP GET"


@dataclass
class StreamReport:
    """The messages servers sent on GET streams, by method."""

    counts: Counter[str] = field(default_factory=Counter)
    requests: set[str] = field(default_factory=set)
    """Methods sent as requests (they carry an id): the client must answer them."""
    member_order: list[str] = field(default_factory=list)
    """The members of the first message, in the order the server wrote them."""

    @property
    def methods(self) -> list[str]:
        """Every method seen, most frequent first."""
        return [method for method, _ in self.counts.most_common()]


def stream_report(exchanges: Iterable[Exchange]) -> StreamReport:
    """What the server sent on the client's GET stream, across the recordings."""
    report = StreamReport()
    for exchange in exchanges:
        if not exchange.endpoint.startswith(STREAM_PREFIX):
            continue
        for event in exchange.stream:
            try:
                message = jsonfmt.loads(event.data or "")
            except ValueError:
                continue  # a priming event, a comment
            if not isinstance(message, dict) or not isinstance(message.get("method"), str):
                continue
            method = str(message["method"])
            report.counts[method] += 1
            if "id" in message:
                report.requests.add(method)
            if not report.member_order:
                report.member_order = list(message)
    return report
