from __future__ import annotations

from datetime import date

import pytest

from fauxcall_reference_api import market
from fauxcall_reference_api.catalog import Exchange, default_catalog
from fauxcall_reference_api.timefmt import iso_z
from fauxcall_reference_api.views import QUOTE_DELAY_NS, market_status_out, quote_as_of

from .helpers import at

CATALOG = default_catalog()
NY = "America/New_York"
TOKYO = "Asia/Tokyo"
HK = "Asia/Hong_Kong"
LONDON = "Europe/London"
PARIS = "Europe/Paris"


def exchange(code: str) -> Exchange:
    found = CATALOG.exchange(code)
    assert found is not None
    return found


def utc(instant_ns: int) -> str:
    return iso_z(instant_ns, 0)


@pytest.mark.parametrize(
    ("code", "instant", "state"),
    [
        # Regular US session [09:30, 16:00) New York time, both exchanges.
        ("XNYS", at(2026, 10, 9, 9, 29, 59, tz=NY, nanos=999_999_999), "closed"),
        ("XNYS", at(2026, 10, 9, 9, 30, tz=NY), "open"),
        ("XNAS", at(2026, 10, 9, 15, 59, 59, tz=NY), "open"),
        ("XNAS", at(2026, 10, 9, 16, 0, tz=NY), "closed"),
        ("XNYS", at(2026, 10, 10, 12, 0, tz=NY), "closed"),  # Saturday
        ("XNYS", at(2026, 11, 26, 12, 0, tz=NY), "closed"),  # Thanksgiving
        ("XNYS", at(2026, 7, 3, 12, 0, tz=NY), "closed"),  # Independence Day observed
        ("XNYS", at(2026, 11, 27, 12, 59, 59, tz=NY), "open"),  # early-close day
        ("XNYS", at(2026, 11, 27, 13, 0, tz=NY), "closed"),
        ("XNAS", at(2026, 12, 24, 13, 0, tz=NY), "closed"),
        ("XNYS", at(2026, 11, 2, 9, 30, tz=NY), "open"),  # first EST day
        # Tokyo: [09:00, 11:30) and [12:30, 15:30), lunch break in between.
        ("XTKS", at(2026, 10, 9, 9, 0, tz=TOKYO), "open"),
        ("XTKS", at(2026, 10, 9, 11, 30, tz=TOKYO), "closed"),
        ("XTKS", at(2026, 10, 9, 12, 29, 59, tz=TOKYO), "closed"),
        ("XTKS", at(2026, 10, 9, 12, 30, tz=TOKYO), "open"),
        ("XTKS", at(2026, 10, 9, 15, 30, tz=TOKYO), "closed"),
        ("XTKS", at(2026, 10, 12, 10, 0, tz=TOKYO), "closed"),  # Sports Day
        # Hong Kong: [09:30, 12:00) and [13:00, 16:00).
        ("XHKG", at(2026, 10, 9, 11, 59, tz=HK), "open"),
        ("XHKG", at(2026, 10, 9, 12, 30, tz=HK), "closed"),
        ("XHKG", at(2026, 10, 9, 13, 0, tz=HK), "open"),
        ("XHKG", at(2026, 10, 19, 10, 0, tz=HK), "closed"),  # Chung Yeung (observed)
        # London [08:00, 16:30) and Paris [09:00, 17:30), across the DST change on 25 Oct.
        ("XLON", at(2026, 10, 9, 16, 29, tz=LONDON), "open"),
        ("XLON", at(2026, 10, 9, 16, 30, tz=LONDON), "closed"),
        ("XLON", at(2026, 10, 26, 8, 0, tz=LONDON), "open"),
        ("XLON", at(2026, 12, 28, 10, 0, tz=LONDON), "closed"),  # Boxing Day (substitute)
        ("XPAR", at(2026, 10, 9, 17, 29, tz=PARIS), "open"),
        ("XPAR", at(2026, 10, 9, 17, 30, tz=PARIS), "closed"),
        ("XPAR", at(2026, 5, 1, 12, 0, tz=PARIS), "closed"),  # Labour Day
    ],
)
def test_market_state(code: str, instant: int, state: str) -> None:
    assert market.market_state(exchange(code), instant) == state


@pytest.mark.parametrize(
    ("code", "instant", "expected_close"),
    [
        # Closed exactly at the close: the close itself is the last close.
        ("XNYS", at(2026, 10, 9, 16, 0, tz=NY), "2026-10-09T20:00:00Z"),
        # Before the open: the previous trading day's close.
        ("XNYS", at(2026, 10, 9, 9, 29, tz=NY), "2026-10-08T20:00:00Z"),
        # Weekend: Friday's close.
        ("XNYS", at(2026, 10, 11, 12, 0, tz=NY), "2026-10-09T20:00:00Z"),
        # Holiday: the day before.
        ("XNYS", at(2026, 11, 26, 12, 0, tz=NY), "2026-11-25T21:00:00Z"),
        # Early close day, after 13:00 New York.
        ("XNYS", at(2026, 11, 27, 15, 0, tz=NY), "2026-11-27T18:00:00Z"),
        # The Monday after the early close.
        ("XNAS", at(2026, 11, 30, 8, 0, tz=NY), "2026-11-27T18:00:00Z"),
        # Christmas (holiday) after the Christmas Eve early close.
        ("XNYS", at(2026, 12, 25, 12, 0, tz=NY), "2026-12-24T18:00:00Z"),
        # DST: Monday 2 Nov before the open, last close was Friday 30 Oct 16:00 EDT.
        ("XNYS", at(2026, 11, 2, 9, 0, tz=NY), "2026-10-30T20:00:00Z"),
        # New Year's Day 2026 (2025 has no holidays in the data: 31 Dec 2025 traded).
        ("XNYS", at(2026, 1, 1, 12, 0, tz=NY), "2025-12-31T21:00:00Z"),
        # Tokyo lunch break: the morning session's close.
        ("XTKS", at(2026, 10, 9, 12, 0, tz=TOKYO), "2026-10-09T02:30:00Z"),
        # Tokyo after the Monday holiday, before Tuesday's open.
        ("XTKS", at(2026, 10, 13, 8, 0, tz=TOKYO), "2026-10-09T06:30:00Z"),
        # Hong Kong lunch break.
        ("XHKG", at(2026, 10, 9, 12, 30, tz=HK), "2026-10-09T04:00:00Z"),
        # London after Easter Monday.
        ("XLON", at(2026, 4, 7, 7, 0, tz=LONDON), "2026-04-02T15:30:00Z"),
    ],
)
def test_last_close(code: str, instant: int, expected_close: str) -> None:
    assert utc(market.last_close(exchange(code), instant)) == expected_close


@pytest.mark.parametrize(
    ("code", "instant", "expected_open"),
    [
        ("XNYS", at(2026, 10, 9, 9, 29, tz=NY), "2026-10-09T13:30:00Z"),
        ("XNYS", at(2026, 10, 9, 9, 30, tz=NY), "2026-10-12T13:30:00Z"),  # open now: next session
        ("XNYS", at(2026, 10, 30, 17, 0, tz=NY), "2026-11-02T14:30:00Z"),  # across DST
        ("XNYS", at(2026, 11, 25, 17, 0, tz=NY), "2026-11-27T14:30:00Z"),  # skips Thanksgiving
        ("XTKS", at(2026, 10, 10, 12, 0, tz=TOKYO), "2026-10-13T00:00:00Z"),  # skips Sports Day
        ("XTKS", at(2026, 10, 9, 12, 0, tz=TOKYO), "2026-10-09T03:30:00Z"),  # after lunch
        ("XTKS", at(2026, 9, 18, 16, 0, tz=TOKYO), "2026-09-24T00:00:00Z"),  # Silver Week
        ("XHKG", at(2026, 2, 16, 17, 0, tz=HK), "2026-02-20T01:30:00Z"),  # Lunar New Year
    ],
)
def test_next_open(code: str, instant: int, expected_open: str) -> None:
    assert utc(market.next_open(exchange(code), instant)) == expected_open


def test_sessions_on_early_close_and_non_trading_days() -> None:
    nyse = exchange("XNYS")
    (session,) = market.sessions_on(nyse, date(2026, 11, 27))
    assert utc(session.open_ns) == "2026-11-27T14:30:00Z"
    assert utc(session.close_ns) == "2026-11-27T18:00:00Z"
    assert market.sessions_on(nyse, date(2026, 11, 26)) == []
    assert market.sessions_on(nyse, date(2026, 10, 10)) == []
    assert market.is_trading_day(nyse, date(2026, 10, 12))  # Columbus Day trades
    assert not market.is_trading_day(exchange("XTKS"), date(2026, 10, 12))


def test_early_close_drops_sessions_that_would_start_after_it() -> None:
    tokyo = exchange("XTKS")
    half_day = Exchange(
        code=tokyo.code,
        name=tokyo.name,
        mic=tokyo.mic,
        timezone=tokyo.timezone,
        currency=tokyo.currency,
        sessions=tokyo.sessions,
        holidays=frozenset(),
        early_closes={date(2026, 10, 9): tokyo.sessions[0].close},
    )
    (session,) = market.sessions_on(half_day, date(2026, 10, 9))
    assert utc(session.close_ns) == "2026-10-09T02:30:00Z"


def test_quote_as_of() -> None:
    nyse = exchange("XNYS")
    now = at(2026, 10, 9, 11, 0, tz=NY)
    assert quote_as_of(nyse, now) == ("open", now - QUOTE_DELAY_NS)
    closed = at(2026, 10, 9, 18, 0, tz=NY)
    assert quote_as_of(nyse, closed) == ("closed", at(2026, 10, 9, 16, 0, tz=NY))


def test_market_status_out() -> None:
    open_status = market_status_out(exchange("XTKS"), at(2026, 10, 9, 10, 0, tz=TOKYO))
    assert open_status.model_dump() == {
        "exchange": "XTKS",
        "name": "Tokyo Stock Exchange",
        "timezone": "Asia/Tokyo",
        "market_state": "open",
        "session_closes_at": "2026-10-09T02:30:00Z",
        "next_open": "2026-10-09T03:30:00Z",
        "last_close": "2026-10-08T06:30:00Z",
    }
    closed_status = market_status_out(exchange("XTKS"), at(2026, 10, 9, 12, 0, tz=TOKYO))
    assert closed_status.market_state == "closed"
    assert closed_status.session_closes_at is None
