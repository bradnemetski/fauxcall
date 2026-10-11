"""Trading sessions and publication schedules at runtime.

:class:`TradingCalendar` answers time questions about a
:class:`~fauxcall.model.calendar.CalendarModel` for any instant (integer nanoseconds since the
epoch): is the market open, when did it last close, when was the last publication? Calendar field
policies build on it, so a ticker's ``time`` tracks "now" during market hours but stays at the last
tick overnight, and an exchange-rate date is that of the last publication.

Sessions are half-open intervals ``[open, close)``: the market is open at the opening instant and
closed at the closing one. Local times are converted on each date separately, so sessions follow
DST transitions. A local time that does not exist on a date (inside a DST gap) is shifted forward by
the gap; an ambiguous one (inside a DST fold) means its first occurrence.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fauxcall.clock import datetime_to_ns, ns_to_datetime
from fauxcall.errors import ModelError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fauxcall.model.calendar import CalendarModel

SEARCH_HORIZON_DAYS: Final = 400
"""How many days searches look backwards or forwards before giving up (returning ``None``)."""

_CACHE_LIMIT: Final = 4096
_ONE_DAY: Final = timedelta(days=1)

type Session = tuple[int, int]
"""A trading session ``[open_ns, close_ns)``."""


class _Day(NamedTuple):
    sessions: tuple[Session, ...]
    publications: tuple[int, ...]


_CLOSED: Final = _Day((), ())


class TradingCalendar:
    """Runtime queries over a calendar: sessions, closes, ticks and publications at an instant.

    The model is read once, at construction. Windows that overlap or touch (after any early close)
    form a single session; an early close never extends a session.

    Args:
        model: The calendar to evaluate.

    Raises:
        ModelError: The model's time zone is unknown, or one of its times carries a UTC offset
            (calendar times are local to the calendar's zone).
    """

    def __init__(self, model: CalendarModel) -> None:
        self._model = model
        self._zone = _zone(model)
        self._weekdays = frozenset(model.weekdays)
        self._holidays = frozenset(model.holidays)
        self._windows = [(_local(model, w.open), _local(model, w.close)) for w in model.sessions]
        self._early_closes = {day: _local(model, t) for day, t in model.early_closes.items()}
        self._publications = [_local(model, t) for t in model.publications]
        self._trades = bool(self._windows and self._weekdays)
        self._publishes = bool(self._publications and self._weekdays)
        self._days: dict[date, _Day] = {}

    @property
    def name(self) -> str:
        """The calendar's name."""
        return self._model.name

    @property
    def zone(self) -> ZoneInfo:
        """The time zone the calendar's local times are expressed in."""
        return self._zone

    @property
    def model(self) -> CalendarModel:
        """The calendar model."""
        return self._model

    # -- days ----------------------------------------------------------------------------------

    def is_business_day(self, day: date) -> bool:
        """Whether ``day`` is an open weekday that is not a holiday."""
        return day.weekday() in self._weekdays and day not in self._holidays

    def sessions_on(self, day: date) -> list[Session]:
        """The sessions of the local date ``day``, in order, as ``[open_ns, close_ns)`` intervals.

        Empty on days that are not business days. An early close truncates the day's windows and
        drops those that would open at or after it.
        """
        return list(self._day(day).sessions)

    # -- instants ------------------------------------------------------------------------------

    def is_open(self, ns: int) -> bool:
        """Whether a session is open at ``ns`` (opening instant included, closing excluded)."""
        return self._current(ns) is not None

    def last_close(self, ns: int) -> int | None:
        """The most recent session close at or before ``ns``.

        While a session is open, that is the close of the session before it.
        """
        found = self._last_close(ns)
        return found[1] if found is not None else None

    def last_open(self, ns: int) -> int | None:
        """The most recent session open at or before ``ns`` (the current session's, if open)."""
        if self._trades:
            for day in self._walk(ns, -_ONE_DAY):
                for open_ns, _ in reversed(self._day(day).sessions):
                    if open_ns <= ns:
                        return open_ns
        return None

    def next_open(self, ns: int) -> int | None:
        """The first session open strictly after ``ns``."""
        if self._trades:
            for day in self._walk(ns, _ONE_DAY):
                for open_ns, _ in self._day(day).sessions:
                    if open_ns > ns:
                        return open_ns
        return None

    def last_tick(self, ns: int, lag_ns: int = 0) -> int | None:
        """When a live feed last ticked as of ``ns``.

        While a session is open: ``ns - lag_ns``, but never before the session opened. While
        closed: the last session close.

        Raises:
            ValueError: ``lag_ns`` is negative.
        """
        if lag_ns < 0:
            raise ValueError(f"lag must not be negative, got {lag_ns} ns")
        current = self._current(ns)
        if current is not None:
            open_ns = current[1][0]
            return max(ns - lag_ns, open_ns)
        return self.last_close(ns)

    def last_publication(self, ns: int) -> int | None:
        """The most recent scheduled publication on a business day at or before ``ns``."""
        if self._publishes:
            for day in self._walk(ns, -_ONE_DAY):
                for published_ns in reversed(self._day(day).publications):
                    if published_ns <= ns:
                        return published_ns
        return None

    def session_date(self, ns: int) -> date | None:
        """The local date of the open session at ``ns``, else of the most recently closed one."""
        current = self._current(ns)
        if current is not None:
            return current[0]
        found = self._last_close(ns)
        return found[0] if found is not None else None

    # -- internals -----------------------------------------------------------------------------

    def _current(self, ns: int) -> tuple[date, Session] | None:
        """The local date and the session open at ``ns``, if any."""
        day = self._local_date(ns)
        for session in self._day(day).sessions:
            if session[0] <= ns < session[1]:
                return day, session
        return None

    def _last_close(self, ns: int) -> tuple[date, int] | None:
        """The local date and instant of the most recent session close at or before ``ns``."""
        if self._trades:
            for day in self._walk(ns, -_ONE_DAY):
                for _, close_ns in reversed(self._day(day).sessions):
                    if close_ns <= ns:
                        return day, close_ns
        return None

    def _walk(self, ns: int, step: timedelta) -> Iterator[date]:
        """The local date of ``ns``, then each day in ``step``'s direction up to the horizon."""
        day = self._local_date(ns)
        for _ in range(SEARCH_HORIZON_DAYS):
            yield day
            day += step
        yield day

    def _local_date(self, ns: int) -> date:
        return ns_to_datetime(ns, self._zone).date()

    def _day(self, day: date) -> _Day:
        cached = self._days.get(day)
        if cached is None:
            cached = self._build_day(day)
            if len(self._days) >= _CACHE_LIMIT:
                self._days.clear()
            self._days[day] = cached
        return cached

    def _build_day(self, day: date) -> _Day:
        if not self.is_business_day(day):
            return _CLOSED
        early = self._early_closes.get(day)
        sessions: list[Session] = []
        for open_time, close_time in self._windows:
            if early is not None and open_time >= early:
                continue
            end = close_time if early is None else min(close_time, early)
            sessions.append((self._instant(day, open_time), self._instant(day, end)))
        publications = sorted(self._instant(day, moment) for moment in self._publications)
        return _Day(_merge(sessions), tuple(publications))

    def _instant(self, day: date, moment: time) -> int:
        return datetime_to_ns(datetime.combine(day, moment, tzinfo=self._zone))


def _zone(model: CalendarModel) -> ZoneInfo:
    try:
        return ZoneInfo(model.zone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ModelError(f"calendar {model.name!r}: unknown time zone {model.zone!r}") from exc


def _local(model: CalendarModel, moment: time) -> time:
    if moment.tzinfo is not None:
        raise ModelError(
            f"calendar {model.name!r}: {moment.isoformat()} has a UTC offset; calendar times are "
            f"local to {model.zone}"
        )
    return moment


def _merge(sessions: list[Session]) -> tuple[Session, ...]:
    """Sort sessions, drop empty ones (collapsed in a DST gap), join those that overlap or touch."""
    merged: list[Session] = []
    for open_ns, close_ns in sorted(sessions):
        if close_ns <= open_ns:
            continue
        if merged and open_ns <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], close_ns))
        else:
            merged.append((open_ns, close_ns))
    return tuple(merged)
