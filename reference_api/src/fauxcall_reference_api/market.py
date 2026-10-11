"""Trading-calendar arithmetic over integer-nanosecond instants.

Rules (see ``GROUND_TRUTH.md``):

* trading days are Monday-Friday in the exchange's time zone, minus its holidays;
* each trading day has the exchange's sessions, each a half-open interval ``[open, close)`` in
  local wall-clock time (so the instant of the close is already *closed*);
* on an early-close day every session is cut at the early-close time (sessions that would start at
  or after it are dropped).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final, Literal

from fauxcall_reference_api.timefmt import from_datetime, to_datetime

if TYPE_CHECKING:
    from fauxcall_reference_api.catalog import Exchange

type MarketState = Literal["open", "closed"]

_SATURDAY: Final = 5
_SCAN_DAYS: Final = 40
"""How far to look for a previous close / next open. No calendar has a longer gap."""


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """A concrete session: ``[open_ns, close_ns)`` as epoch nanoseconds."""

    open_ns: int
    close_ns: int


def is_trading_day(exchange: Exchange, day: date) -> bool:
    """Whether ``day`` (exchange-local) is a weekday that is not a holiday."""
    return day.weekday() < _SATURDAY and day not in exchange.holidays


def sessions_on(exchange: Exchange, day: date) -> list[SessionWindow]:
    """The sessions of ``day`` (exchange-local date), in order; empty on non-trading days."""
    if not is_trading_day(exchange, day):
        return []
    early_close = exchange.early_closes.get(day)
    windows: list[SessionWindow] = []
    for session in exchange.sessions:
        closes = session.close
        if early_close is not None:
            if session.open >= early_close:
                continue
            closes = min(closes, early_close)
        windows.append(
            SessionWindow(_instant(exchange, day, session.open), _instant(exchange, day, closes))
        )
    return windows


def local_date(exchange: Exchange, instant_ns: int) -> date:
    """The exchange-local calendar date of an instant."""
    return to_datetime(instant_ns).astimezone(exchange.zone).date()


def current_session(exchange: Exchange, now_ns: int) -> SessionWindow | None:
    """The session containing ``now_ns``, if the market is open."""
    for window in sessions_on(exchange, local_date(exchange, now_ns)):
        if window.open_ns <= now_ns < window.close_ns:
            return window
    return None


def market_state(exchange: Exchange, now_ns: int) -> MarketState:
    """``open`` while a session contains ``now_ns``, else ``closed``."""
    return "open" if current_session(exchange, now_ns) is not None else "closed"


def last_close(exchange: Exchange, now_ns: int) -> int:
    """The most recent session close at or before ``now_ns``."""
    today = local_date(exchange, now_ns)
    for offset in range(_SCAN_DAYS):
        for window in reversed(sessions_on(exchange, today - timedelta(days=offset))):
            if window.close_ns <= now_ns:
                return window.close_ns
    raise LookupError(f"no session close within {_SCAN_DAYS} days")  # pragma: no cover


def next_open(exchange: Exchange, now_ns: int) -> int:
    """The earliest session open strictly after ``now_ns``."""
    today = local_date(exchange, now_ns)
    for offset in range(_SCAN_DAYS):
        for window in sessions_on(exchange, today + timedelta(days=offset)):
            if window.open_ns > now_ns:
                return window.open_ns
    raise LookupError(f"no session open within {_SCAN_DAYS} days")  # pragma: no cover


def _instant(exchange: Exchange, day: date, wall: time) -> int:
    return from_datetime(datetime.combine(day, wall, tzinfo=exchange.zone))
