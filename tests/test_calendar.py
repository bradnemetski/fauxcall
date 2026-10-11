from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from fauxcall import calendar as calendar_module
from fauxcall.calendar import SEARCH_HORIZON_DAYS, TradingCalendar
from fauxcall.clock import ControlledClock, ManualClock, datetime_to_ns
from fauxcall.errors import ModelError
from fauxcall.model.calendar import CalendarModel, SessionWindow

NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
BERLIN = ZoneInfo("Europe/Berlin")
LONDON = ZoneInfo("Europe/London")
SECOND = 1_000_000_000
MINUTE = 60 * SECOND
EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]

NYSE = CalendarModel(
    name="nyse",
    zone="America/New_York",
    sessions=[SessionWindow(open=time(9, 30), close=time(16))],
    holidays=[date(2026, 1, 1), date(2026, 11, 26), date(2026, 12, 25)],
    early_closes={date(2026, 11, 27): time(13), date(2026, 10, 10): time(13)},
)
TSE = CalendarModel(
    name="tse",
    zone="Asia/Tokyo",
    sessions=[
        SessionWindow(open=time(9), close=time(11, 30)),
        SessionWindow(open=time(12, 30), close=time(15, 30)),
    ],
    holidays=[date(2026, 1, 1), date(2026, 1, 2)],
)
ECB = CalendarModel(
    name="ecb",
    zone="Europe/Berlin",
    publications=[time(16)],
    holidays=[
        date(2026, 1, 1),
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 6),  # Easter Monday
        date(2026, 5, 1),
        date(2026, 12, 25),
        date(2026, 12, 26),
    ],
)


def at(zone: ZoneInfo, stamp: str) -> int:
    """The instant a local wall-clock time like ``"2026-10-09 09:30"`` denotes in ``zone``."""
    return datetime_to_ns(datetime.fromisoformat(stamp).replace(tzinfo=zone))


def utc(stamp: str) -> int:
    return at(ZoneInfo("UTC"), stamp)


@pytest.fixture
def nyse() -> TradingCalendar:
    return TradingCalendar(NYSE)


@pytest.fixture
def tse() -> TradingCalendar:
    return TradingCalendar(TSE)


@pytest.fixture
def ecb() -> TradingCalendar:
    return TradingCalendar(ECB)


def test_properties(nyse: TradingCalendar) -> None:
    assert nyse.name == "nyse"
    assert nyse.zone.key == "America/New_York"
    assert nyse.model is NYSE


# ---------------------------------------------------------------------------
# NYSE-like: one session a day, holidays, an early close
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 9), True),  # Friday
        (date(2026, 10, 10), False),  # Saturday (its early close does not open it)
        (date(2026, 10, 11), False),  # Sunday
        (date(2026, 11, 26), False),  # Thanksgiving
        (date(2026, 11, 27), True),  # early close, still a business day
        (date(2026, 12, 25), False),
        (date(2026, 1, 1), False),
    ],
)
def test_business_days(nyse: TradingCalendar, day: date, expected: bool) -> None:
    assert nyse.is_business_day(day) is expected


def test_sessions_in_summer_and_winter_time(nyse: TradingCalendar) -> None:
    assert nyse.sessions_on(date(2026, 10, 9)) == [
        (utc("2026-10-09 13:30"), utc("2026-10-09 20:00"))
    ]
    assert nyse.sessions_on(date(2026, 12, 1)) == [
        (utc("2026-12-01 14:30"), utc("2026-12-01 21:00"))
    ]


def test_no_sessions_on_weekends_and_holidays(nyse: TradingCalendar) -> None:
    for day in (date(2026, 10, 10), date(2026, 10, 11), date(2026, 11, 26), date(2026, 12, 25)):
        assert nyse.sessions_on(day) == []


def test_early_close_truncates_the_session(nyse: TradingCalendar) -> None:
    assert nyse.sessions_on(date(2026, 11, 27)) == [
        (at(NEW_YORK, "2026-11-27 09:30"), at(NEW_YORK, "2026-11-27 13:00"))
    ]


def test_sessions_on_returns_a_fresh_list(nyse: TradingCalendar) -> None:
    sessions = nyse.sessions_on(date(2026, 10, 9))
    sessions.clear()
    assert len(nyse.sessions_on(date(2026, 10, 9))) == 1


def test_open_is_inclusive_and_close_exclusive(nyse: TradingCalendar) -> None:
    open_ns = at(NEW_YORK, "2026-10-09 09:30")
    close_ns = at(NEW_YORK, "2026-10-09 16:00")
    assert not nyse.is_open(open_ns - 1)
    assert nyse.is_open(open_ns)
    assert nyse.is_open(close_ns - 1)
    assert not nyse.is_open(close_ns)
    assert not nyse.is_open(at(NEW_YORK, "2026-10-10 12:00"))  # Saturday
    assert not nyse.is_open(at(NEW_YORK, "2026-11-26 12:00"))  # Thanksgiving
    assert nyse.is_open(at(NEW_YORK, "2026-11-27 12:59"))
    assert not nyse.is_open(at(NEW_YORK, "2026-11-27 13:00"))  # early close


def test_last_close(nyse: TradingCalendar) -> None:
    friday_close = at(NEW_YORK, "2026-10-09 16:00")
    assert nyse.last_close(friday_close) == friday_close
    assert nyse.last_close(friday_close - 1) == at(NEW_YORK, "2026-10-08 16:00")  # still open
    assert nyse.last_close(at(NEW_YORK, "2026-10-10 12:00")) == friday_close  # Saturday
    assert nyse.last_close(at(NEW_YORK, "2026-10-12 09:29")) == friday_close  # Monday morning
    assert nyse.last_close(at(NEW_YORK, "2026-10-12 10:00")) == friday_close  # Monday, open
    assert nyse.last_close(at(NEW_YORK, "2026-11-26 12:00")) == at(NEW_YORK, "2026-11-25 16:00")
    assert nyse.last_close(at(NEW_YORK, "2026-11-27 12:00")) == at(NEW_YORK, "2026-11-25 16:00")
    assert nyse.last_close(at(NEW_YORK, "2026-11-27 14:00")) == at(NEW_YORK, "2026-11-27 13:00")
    assert nyse.last_close(at(NEW_YORK, "2026-01-01 12:00")) == at(NEW_YORK, "2025-12-31 16:00")


def test_last_open(nyse: TradingCalendar) -> None:
    open_ns = at(NEW_YORK, "2026-10-09 09:30")
    assert nyse.last_open(open_ns) == open_ns
    assert nyse.last_open(open_ns - 1) == at(NEW_YORK, "2026-10-08 09:30")
    assert nyse.last_open(at(NEW_YORK, "2026-10-09 15:00")) == open_ns
    assert nyse.last_open(at(NEW_YORK, "2026-10-11 20:00")) == open_ns  # Sunday


def test_next_open(nyse: TradingCalendar) -> None:
    monday_open = at(NEW_YORK, "2026-10-12 09:30")
    assert nyse.next_open(at(NEW_YORK, "2026-10-09 16:00")) == monday_open
    assert nyse.next_open(at(NEW_YORK, "2026-10-09 10:00")) == monday_open  # while open
    assert nyse.next_open(monday_open - 1) == monday_open
    assert nyse.next_open(monday_open) == at(NEW_YORK, "2026-10-13 09:30")  # strictly after
    assert nyse.next_open(at(NEW_YORK, "2026-11-25 16:00")) == at(NEW_YORK, "2026-11-27 09:30")
    assert nyse.next_open(at(NEW_YORK, "2026-12-24 17:00")) == at(NEW_YORK, "2026-12-28 09:30")


def test_last_tick_tracks_now_while_open(nyse: TradingCalendar) -> None:
    now = at(NEW_YORK, "2026-10-09 11:00")
    assert nyse.last_tick(now) == now
    assert nyse.last_tick(now, lag_ns=15 * MINUTE) == at(NEW_YORK, "2026-10-09 10:45")


def test_last_tick_never_precedes_the_open(nyse: TradingCalendar) -> None:
    open_ns = at(NEW_YORK, "2026-10-09 09:30")
    lag = 15 * MINUTE
    assert nyse.last_tick(open_ns, lag_ns=lag) == open_ns
    assert nyse.last_tick(open_ns + 5 * MINUTE, lag_ns=lag) == open_ns
    assert nyse.last_tick(open_ns + 20 * MINUTE, lag_ns=lag) == open_ns + 5 * MINUTE


def test_last_tick_is_frozen_while_closed(nyse: TradingCalendar) -> None:
    close = at(NEW_YORK, "2026-10-09 16:00")
    assert nyse.last_tick(close - 1) == close - 1
    assert nyse.last_tick(close) == close
    assert nyse.last_tick(close, lag_ns=15 * MINUTE) == close
    assert nyse.last_tick(at(NEW_YORK, "2026-10-10 03:00")) == close
    assert nyse.last_tick(at(NEW_YORK, "2026-10-12 09:29:59.999999")) == close


def test_last_tick_rejects_a_negative_lag(nyse: TradingCalendar) -> None:
    with pytest.raises(ValueError, match="lag"):
        nyse.last_tick(at(NEW_YORK, "2026-10-09 11:00"), lag_ns=-1)


def test_session_date(nyse: TradingCalendar) -> None:
    assert nyse.session_date(at(NEW_YORK, "2026-10-09 10:00")) == date(2026, 10, 9)
    assert nyse.session_date(utc("2026-10-10 00:00")) == date(2026, 10, 9)  # Saturday in UTC
    assert nyse.session_date(at(NEW_YORK, "2026-10-09 23:59:59.999999") + 999) == date(2026, 10, 9)
    assert nyse.session_date(at(NEW_YORK, "2026-10-13 02:00")) == date(2026, 10, 12)  # overnight
    assert nyse.session_date(at(NEW_YORK, "2026-11-26 12:00")) == date(2026, 11, 25)
    assert nyse.session_date(at(NEW_YORK, "2026-11-27 12:00")) == date(2026, 11, 27)


def test_no_publications_on_a_trading_calendar(nyse: TradingCalendar) -> None:
    assert nyse.last_publication(at(NEW_YORK, "2026-10-09 17:00")) is None


def test_ticker_time_through_the_closing_bell(nyse: TradingCalendar) -> None:
    base = ManualClock(at(NEW_YORK, "2026-10-09 15:58"))
    clock = ControlledClock(base)
    clock.scale(60)  # a simulated minute per real second
    ticks = []
    for _ in range(4):
        ticks.append(nyse.last_tick(clock.now_ns()))
        base.advance(1)
    close = at(NEW_YORK, "2026-10-09 16:00")
    assert ticks == [
        at(NEW_YORK, "2026-10-09 15:58"),
        at(NEW_YORK, "2026-10-09 15:59"),
        close,
        close,
    ]
    clock.travel_to(at(NEW_YORK, "2026-10-12 09:31"))
    assert nyse.last_tick(clock.now_ns()) == at(NEW_YORK, "2026-10-12 09:31")


# ---------------------------------------------------------------------------
# DST transitions (America/New_York: 2026-03-08 and 2026-11-01)
# ---------------------------------------------------------------------------


def test_nyse_across_the_spring_transition(nyse: TradingCalendar) -> None:
    sunday = at(NEW_YORK, "2026-03-08 12:00")
    assert nyse.last_close(sunday) == utc("2026-03-06 21:00")  # Friday 16:00 EST
    assert nyse.next_open(sunday) == utc("2026-03-09 13:30")  # Monday 09:30 EDT
    assert nyse.session_date(sunday) == date(2026, 3, 6)


def test_nyse_across_the_autumn_transition(nyse: TradingCalendar) -> None:
    sunday = at(NEW_YORK, "2026-11-01 12:00")
    assert nyse.last_close(sunday) == utc("2026-10-30 20:00")  # Friday 16:00 EDT
    assert nyse.next_open(sunday) == utc("2026-11-02 14:30")  # Monday 09:30 EST


def test_sessions_on_dst_transition_dates() -> None:
    nightly = TradingCalendar(
        CalendarModel(
            name="nightly",
            zone="America/New_York",
            weekdays=EVERY_DAY,
            sessions=[SessionWindow(open=time(1), close=time(3))],
        )
    )
    assert nightly.sessions_on(date(2026, 3, 7)) == [
        (utc("2026-03-07 06:00"), utc("2026-03-07 08:00"))
    ]
    # 02:00-03:00 does not exist: the session lasts one hour.
    assert nightly.sessions_on(date(2026, 3, 8)) == [
        (utc("2026-03-08 06:00"), utc("2026-03-08 07:00"))
    ]
    # 01:00-02:00 happens twice: the session lasts three hours.
    assert nightly.sessions_on(date(2026, 11, 1)) == [
        (utc("2026-11-01 05:00"), utc("2026-11-01 08:00"))
    ]
    repeated = datetime_to_ns(datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NEW_YORK))
    assert nightly.is_open(repeated)
    assert nightly.last_open(repeated) == utc("2026-11-01 05:00")
    assert nightly.session_date(repeated) == date(2026, 11, 1)
    assert nightly.last_close(repeated) == utc("2026-10-31 07:00")


def test_a_window_inside_a_dst_gap_is_dropped() -> None:
    gap = TradingCalendar(
        CalendarModel(
            name="gap",
            zone="America/New_York",
            weekdays=EVERY_DAY,
            sessions=[SessionWindow(open=time(2, 30), close=time(3, 15))],
        )
    )
    assert gap.sessions_on(date(2026, 3, 8)) == []
    assert gap.sessions_on(date(2026, 3, 9)) == [
        (at(NEW_YORK, "2026-03-09 02:30"), at(NEW_YORK, "2026-03-09 03:15"))
    ]
    assert gap.last_close(at(NEW_YORK, "2026-03-08 12:00")) == at(NEW_YORK, "2026-03-07 03:15")


def test_windows_overlapping_after_a_dst_gap_are_merged() -> None:
    split = TradingCalendar(
        CalendarModel(
            name="split",
            zone="America/New_York",
            weekdays=EVERY_DAY,
            sessions=[
                SessionWindow(open=time(1), close=time(2, 45)),
                SessionWindow(open=time(3), close=time(4)),
            ],
        )
    )
    assert split.sessions_on(date(2026, 3, 8)) == [
        (utc("2026-03-08 06:00"), utc("2026-03-08 08:00"))
    ]
    assert split.sessions_on(date(2026, 3, 9)) == [
        (at(NEW_YORK, "2026-03-09 01:00"), at(NEW_YORK, "2026-03-09 02:45")),
        (at(NEW_YORK, "2026-03-09 03:00"), at(NEW_YORK, "2026-03-09 04:00")),
    ]


def test_overlapping_and_touching_windows_form_one_session() -> None:
    calendar = TradingCalendar(
        CalendarModel(
            name="messy",
            zone="UTC",
            sessions=[
                SessionWindow(open=time(12), close=time(14)),
                SessionWindow(open=time(9), close=time(12)),
                SessionWindow(open=time(13), close=time(13, 30)),
                SessionWindow(open=time(15), close=time(16)),
            ],
        )
    )
    assert calendar.sessions_on(date(2026, 10, 9)) == [
        (utc("2026-10-09 09:00"), utc("2026-10-09 14:00")),
        (utc("2026-10-09 15:00"), utc("2026-10-09 16:00")),
    ]


# ---------------------------------------------------------------------------
# Lunch break (Asia/Tokyo)
# ---------------------------------------------------------------------------


def test_lunch_break(tse: TradingCalendar) -> None:
    morning = (at(TOKYO, "2026-10-09 09:00"), at(TOKYO, "2026-10-09 11:30"))
    afternoon = (at(TOKYO, "2026-10-09 12:30"), at(TOKYO, "2026-10-09 15:30"))
    lunch = at(TOKYO, "2026-10-09 12:00")
    assert tse.sessions_on(date(2026, 10, 9)) == [morning, afternoon]
    assert tse.is_open(morning[1] - 1)
    assert not tse.is_open(morning[1])
    assert not tse.is_open(lunch)
    assert tse.is_open(afternoon[0])
    assert tse.last_close(lunch) == morning[1]
    assert tse.last_open(lunch) == morning[0]
    assert tse.next_open(lunch) == afternoon[0]
    assert tse.next_open(at(TOKYO, "2026-10-09 10:00")) == afternoon[0]
    assert tse.last_tick(lunch, lag_ns=MINUTE) == morning[1]
    assert tse.session_date(lunch) == date(2026, 10, 9)


def test_afternoon_session(tse: TradingCalendar) -> None:
    afternoon_open = at(TOKYO, "2026-10-09 12:30")
    now = at(TOKYO, "2026-10-09 13:00")
    assert tse.last_close(now) == at(TOKYO, "2026-10-09 11:30")  # the morning session's close
    assert tse.last_open(now) == afternoon_open
    assert tse.last_tick(afternoon_open + SECOND // 2, lag_ns=SECOND) == afternoon_open
    assert tse.last_tick(now, lag_ns=SECOND) == now - SECOND
    assert tse.session_date(at(TOKYO, "2026-10-09 23:00")) == date(2026, 10, 9)


def test_tokyo_dates_run_ahead_of_utc(tse: TradingCalendar) -> None:
    sunday_utc = utc("2026-10-11 23:30")  # already Monday 08:30 in Tokyo
    assert tse.next_open(sunday_utc) == utc("2026-10-12 00:00")
    assert tse.last_close(sunday_utc) == at(TOKYO, "2026-10-09 15:30")
    assert tse.last_close(at(TOKYO, "2026-01-05 08:00")) == at(TOKYO, "2025-12-31 15:30")


@pytest.mark.parametrize(
    ("early", "expected"),
    [
        (time(9), []),
        (time(11), [("09:00", "11:00")]),
        (time(12), [("09:00", "11:30")]),
        (time(12, 30), [("09:00", "11:30")]),  # the afternoon would open at the close: dropped
        (time(13), [("09:00", "11:30"), ("12:30", "13:00")]),
        (time(16), [("09:00", "11:30"), ("12:30", "15:30")]),  # never extends a session
    ],
)
def test_early_close_with_a_lunch_break(early: time, expected: list[tuple[str, str]]) -> None:
    day = date(2026, 12, 30)
    calendar = TradingCalendar(TSE.model_copy(update={"early_closes": {day: early}}))
    assert calendar.is_business_day(day)
    assert calendar.sessions_on(day) == [
        (at(TOKYO, f"2026-12-30 {start}"), at(TOKYO, f"2026-12-30 {end}"))
        for start, end in expected
    ]


# ---------------------------------------------------------------------------
# Publications (ECB reference rates: 16:00 Europe/Berlin on TARGET business days)
# ---------------------------------------------------------------------------


def test_last_publication(ecb: TradingCalendar) -> None:
    friday = at(BERLIN, "2026-10-09 16:00")
    monday = at(BERLIN, "2026-10-12 16:00")
    assert friday == utc("2026-10-09 14:00")
    assert ecb.last_publication(at(BERLIN, "2026-10-10 12:00")) == friday  # Saturday
    assert ecb.last_publication(at(BERLIN, "2026-10-12 15:59")) == friday
    assert ecb.last_publication(monday - 1) == friday
    assert ecb.last_publication(monday) == monday
    assert ecb.last_publication(at(BERLIN, "2026-04-07 10:00")) == at(BERLIN, "2026-04-02 16:00")
    assert ecb.last_publication(at(BERLIN, "2026-12-28 12:00")) == utc("2026-12-24 15:00")


def test_publications_follow_dst(ecb: TradingCalendar) -> None:
    assert ecb.last_publication(at(BERLIN, "2026-03-29 12:00")) == utc("2026-03-27 15:00")  # CET
    assert ecb.last_publication(at(BERLIN, "2026-03-30 18:00")) == utc("2026-03-30 14:00")  # CEST


def test_publication_calendar_has_no_sessions(ecb: TradingCalendar) -> None:
    now = at(BERLIN, "2026-10-09 17:00")
    assert ecb.sessions_on(date(2026, 10, 9)) == []
    assert not ecb.is_open(now)
    assert ecb.last_close(now) is None
    assert ecb.last_open(now) is None
    assert ecb.next_open(now) is None
    assert ecb.last_tick(now) is None
    assert ecb.session_date(now) is None


def test_several_publications_a_day() -> None:
    fixings = TradingCalendar(
        CalendarModel(name="fixings", zone="Europe/London", publications=[time(16), time(10, 30)])
    )
    assert fixings.last_publication(at(LONDON, "2026-10-09 12:00")) == at(
        LONDON, "2026-10-09 10:30"
    )
    assert fixings.last_publication(at(LONDON, "2026-10-09 17:00")) == at(
        LONDON, "2026-10-09 16:00"
    )
    assert fixings.last_publication(at(LONDON, "2026-10-12 09:00")) == at(
        LONDON, "2026-10-09 16:00"
    )


# ---------------------------------------------------------------------------
# Empty calendars and the search horizon
# ---------------------------------------------------------------------------


def _assert_nothing_found(calendar: TradingCalendar, now: int) -> None:
    assert not calendar.is_open(now)
    assert calendar.last_close(now) is None
    assert calendar.last_open(now) is None
    assert calendar.next_open(now) is None
    assert calendar.last_tick(now) is None
    assert calendar.last_publication(now) is None
    assert calendar.session_date(now) is None


def test_calendar_without_open_weekdays() -> None:
    never = TradingCalendar(
        CalendarModel(
            name="never",
            zone="UTC",
            weekdays=[],
            sessions=[SessionWindow(open=time(9), close=time(17))],
            publications=[time(16)],
        )
    )
    assert not never.is_business_day(date(2026, 10, 9))
    assert never.sessions_on(date(2026, 10, 9)) == []
    _assert_nothing_found(never, utc("2026-10-09 12:00"))


def test_calendar_without_sessions_or_publications() -> None:
    empty = TradingCalendar(CalendarModel(name="empty", zone="UTC"))
    assert empty.is_business_day(date(2026, 10, 9))
    assert empty.sessions_on(date(2026, 10, 9)) == []
    _assert_nothing_found(empty, utc("2026-10-09 12:00"))


def _closed_for(first: date, days: int) -> TradingCalendar:
    """A daily 09:00-17:00 market (publishing at 16:00) closed for ``days`` days from ``first``."""
    return TradingCalendar(
        CalendarModel(
            name="closed",
            zone="UTC",
            weekdays=EVERY_DAY,
            sessions=[SessionWindow(open=time(9), close=time(17))],
            publications=[time(16)],
            holidays=[first + timedelta(days=offset) for offset in range(days)],
        )
    )


def test_backward_searches_reach_the_horizon() -> None:
    today = date(2026, 10, 9)
    now = utc("2026-10-09 12:00")
    edge = today - timedelta(days=SEARCH_HORIZON_DAYS)
    reachable = _closed_for(edge + timedelta(days=1), SEARCH_HORIZON_DAYS)
    stamp = edge.isoformat()
    assert reachable.last_close(now) == utc(f"{stamp} 17:00")
    assert reachable.last_open(now) == utc(f"{stamp} 09:00")
    assert reachable.last_tick(now) == utc(f"{stamp} 17:00")
    assert reachable.last_publication(now) == utc(f"{stamp} 16:00")
    assert reachable.session_date(now) == edge
    beyond = _closed_for(edge, SEARCH_HORIZON_DAYS + 1)
    assert beyond.last_close(now) is None
    assert beyond.last_open(now) is None
    assert beyond.last_tick(now) is None
    assert beyond.last_publication(now) is None
    assert beyond.session_date(now) is None
    assert beyond.next_open(now) == utc("2026-10-10 09:00")  # tomorrow is open again


def test_forward_searches_reach_the_horizon() -> None:
    today = date(2026, 10, 9)
    now = utc("2026-10-09 12:00")
    edge = today + timedelta(days=SEARCH_HORIZON_DAYS)
    assert _closed_for(today, SEARCH_HORIZON_DAYS).next_open(now) == utc(f"{edge} 09:00")
    assert _closed_for(today, SEARCH_HORIZON_DAYS + 1).next_open(now) is None


# ---------------------------------------------------------------------------
# Invalid models and caching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "", "America", "../etc/passwd"])
def test_unknown_zones_are_rejected(zone: str) -> None:
    with pytest.raises(ValidationError, match="unknown time zone"):
        CalendarModel(name="bad", zone=zone)
    # Models built without validation are still caught by the engine.
    with pytest.raises(ModelError, match="unknown time zone"):
        TradingCalendar(CalendarModel.model_construct(name="bad", zone=zone))


@pytest.mark.parametrize(
    "fields",
    [
        {"publications": ["16:00+01:00"]},
        {"sessions": [{"open": "09:30Z", "close": "16:00Z"}]},
        {"early_closes": {"2026-11-27": "13:00-05:00"}},
    ],
)
def test_times_with_utc_offsets_are_rejected(fields: dict[str, object]) -> None:
    payload = {"name": "bad", "zone": "Europe/Berlin", **fields}
    with pytest.raises(ValidationError, match="local to 'zone'"):
        CalendarModel.model_validate(payload)
    # Without validation the engine still refuses to guess.
    unchecked = CalendarModel.model_construct(
        **{
            **CalendarModel.model_construct(name="bad", zone="Europe/Berlin").__dict__,
            **_construct_fields(fields),
        }
    )
    with pytest.raises(ModelError, match="local to Europe/Berlin"):
        TradingCalendar(unchecked)


def _construct_fields(fields: dict[str, object]) -> dict[str, object]:
    """Parse the time strings the way validation would, but keep their offsets."""
    from datetime import date as date_cls  # noqa: PLC0415
    from datetime import time as time_cls  # noqa: PLC0415

    parsed: dict[str, object] = {}
    for key, value in fields.items():
        if key == "publications":
            parsed[key] = [time_cls.fromisoformat(str(v)) for v in value]  # type: ignore[attr-defined]
        elif key == "sessions":
            parsed[key] = [
                SessionWindow.model_construct(
                    open=time_cls.fromisoformat(str(w["open"])),
                    close=time_cls.fromisoformat(str(w["close"])),
                )
                for w in value  # type: ignore[attr-defined]
            ]
        else:
            parsed[key] = {
                date_cls.fromisoformat(str(k)): time_cls.fromisoformat(str(v))
                for k, v in value.items()  # type: ignore[attr-defined]
            }
    return parsed


def test_day_cache_is_bounded(nyse: TradingCalendar, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calendar_module, "_CACHE_LIMIT", 2)
    monday = date(2026, 10, 5)
    for offset in range(5):
        nyse.sessions_on(monday + timedelta(days=offset))
        assert len(nyse._days) <= 2
    assert nyse.sessions_on(monday) == [
        (at(NEW_YORK, "2026-10-05 09:30"), at(NEW_YORK, "2026-10-05 16:00"))
    ]


def test_aware_utc_helper_matches_datetime() -> None:
    assert utc("2026-10-09 13:30") == datetime_to_ns(datetime(2026, 10, 9, 13, 30, tzinfo=UTC))
