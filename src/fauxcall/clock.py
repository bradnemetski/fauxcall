"""The fake's sense of time: real, frozen, shifted or accelerated clocks, isolated per session.

Instants are integer nanoseconds since the Unix epoch. A fake asks one :class:`Clock` for "now" and
derives every time-dependent value from it, so controlling that clock controls everything at once:
``GET /current_time`` tracks it, calendar fields follow it, async jobs progress with it.

:class:`ControlledClock` derives its time from a base clock (real time by default) in one of four
modes:

* ``real`` — the base clock unchanged (the default, so ``GET /current_time`` is the actual time);
* ``frozen`` — a fixed instant that only moves when told to;
* ``offset`` — the base clock shifted by a constant (time travel; time keeps flowing);
* ``scaled`` — time flows ``rate`` times as fast from an anchor (a trading day in minutes).

:class:`ClockRegistry` lets an evaluation session control its own clock without affecting others.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal, Protocol, Self

from pydantic import AwareDatetime, Field

from fauxcall.model.common import Model
from fauxcall.timefmt import NS_PER_SECOND

if TYPE_CHECKING:
    from fauxcall.config import ClockSettings

ClockMode = Literal["real", "frozen", "offset", "scaled"]
"""How a :class:`ControlledClock` derives its time from its base clock."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NS_PER_MICROSECOND: Final = 1_000
_MICROSECOND: Final = timedelta(microseconds=1)


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def seconds_to_ns(seconds: float) -> int:
    """Convert seconds to integer nanoseconds, rounding to the nearest nanosecond.

    The decimal spelling of ``seconds`` is used, so ``0.1`` is exactly 100 000 000 ns and large
    values keep their nanoseconds.

    Raises:
        ValueError: ``seconds`` is NaN or infinite.
    """
    if not math.isfinite(seconds):
        raise ValueError(f"seconds must be finite, got {seconds!r}")
    return round(Decimal(str(seconds)) * NS_PER_SECOND)


def ns_to_datetime(ns: int, tz: tzinfo = UTC) -> datetime:
    """Convert nanoseconds since the epoch to an aware datetime in ``tz``.

    Datetimes hold microseconds, so extra nanoseconds are dropped (rounding towards the past).
    """
    return (_EPOCH + timedelta(microseconds=ns // _NS_PER_MICROSECOND)).astimezone(tz)


def datetime_to_ns(dt: datetime) -> int:
    """Convert an aware datetime to nanoseconds since the epoch, keeping microseconds exactly.

    Raises:
        ValueError: ``dt`` is naive.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"expected a timezone-aware datetime, got {dt!r}")
    return (dt - _EPOCH) // _MICROSECOND * _NS_PER_MICROSECOND


def _check_rate(rate: float) -> float:
    if not (math.isfinite(rate) and rate > 0):
        raise ValueError(f"rate must be a finite number > 0, got {rate!r}")
    return float(rate)


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------


class Clock(Protocol):
    """Anything that tells the time as integer nanoseconds since the Unix epoch."""

    def now_ns(self) -> int:
        """The current instant."""
        ...


class SystemClock:
    """The real wall clock (:func:`time.time_ns`)."""

    __slots__ = ()

    def now_ns(self) -> int:
        """The current real time."""
        return time.time_ns()


class ManualClock:
    """A clock that only moves when told to: inject it in tests instead of real time.

    Args:
        start_ns: The clock's initial instant.
    """

    __slots__ = ("_now",)

    def __init__(self, start_ns: int) -> None:
        self._now = start_ns

    def now_ns(self) -> int:
        """The clock's current instant."""
        return self._now

    def set(self, ns: int) -> None:
        """Jump to ``ns`` (backwards too)."""
        self._now = ns

    def advance(self, seconds: float) -> None:
        """Move by ``seconds`` (negative values move backwards)."""
        self._now += seconds_to_ns(seconds)


class ClockState(Model):
    """A snapshot of a :class:`ControlledClock`, as the admin API reports it."""

    mode: ClockMode
    now: AwareDatetime = Field(description="The clock's current time (UTC).")
    offset_seconds: float = Field(
        default=0.0, description="The clock's time minus real (base clock) time, in seconds."
    )
    rate: float = Field(
        default=1.0,
        description="Clock seconds per real second: 1 (real, offset), 0 (frozen) or the scale.",
    )
    frozen_at: AwareDatetime | None = Field(
        default=None, description="The frozen instant ('frozen' only)."
    )
    anchor_real: AwareDatetime | None = Field(
        default=None, description="Real time when scaling started ('scaled' only)."
    )
    anchor_simulated: AwareDatetime | None = Field(
        default=None, description="The clock's time when scaling started ('scaled' only)."
    )


@dataclass(frozen=True, slots=True)
class _Timeline:
    """How the clock's time derives from base time.

    Immutable, so readers never observe a half-applied change: writers swap in a new one.
    """

    mode: ClockMode = "real"
    frozen_ns: int = 0
    offset_ns: int = 0
    anchor_real_ns: int = 0
    anchor_simulated_ns: int = 0
    rate: float = 1.0

    def at(self, base_ns: int) -> int:
        """The clock's time when the base clock reads ``base_ns``."""
        if self.mode == "frozen":
            return self.frozen_ns
        if self.mode == "offset":
            return base_ns + self.offset_ns
        if self.mode == "scaled":
            # Exact integer arithmetic: the rate as a ratio, so the clock never jitters.
            numerator, denominator = self.rate.as_integer_ratio()
            elapsed = base_ns - self.anchor_real_ns
            return self.anchor_simulated_ns + elapsed * numerator // denominator
        return base_ns

    @property
    def effective_rate(self) -> float:
        """Clock seconds per base-clock second."""
        if self.mode == "frozen":
            return 0.0
        return self.rate if self.mode == "scaled" else 1.0


def _timeline_for(settings: ClockSettings, base_ns: int) -> _Timeline:
    """The timeline ``settings`` describe when the base clock reads ``base_ns``."""
    start_ns = base_ns if settings.at is None else datetime_to_ns(settings.at)
    match settings.mode:
        case "frozen":
            return _Timeline(mode="frozen", frozen_ns=start_ns)
        case "offset":
            if settings.at is None:
                return _Timeline(mode="offset", offset_ns=seconds_to_ns(settings.offset_seconds))
            return _Timeline(mode="offset", offset_ns=start_ns - base_ns)
        case "scaled":
            return _Timeline(
                mode="scaled",
                anchor_real_ns=base_ns,
                anchor_simulated_ns=start_ns,
                rate=_check_rate(settings.rate),
            )
        case _:
            return _Timeline()


class ControlledClock:
    """A clock that can be frozen, shifted or accelerated; implements :class:`Clock`.

    Its time derives from a base clock (real time by default). Operations keep time continuous
    where that makes sense: freezing, scaling and advancing start from the clock's current time
    rather than from real time. Safe to share between threads: readers see an immutable timeline
    that writers replace under a lock.

    Args:
        base: The clock this one derives its time from (default: :class:`SystemClock`).
    """

    def __init__(self, base: Clock | None = None) -> None:
        self._base: Clock = base if base is not None else SystemClock()
        self._timeline = _Timeline()
        self._settings: ClockSettings | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: ClockSettings, base: Clock | None = None) -> Self:
        """Create a clock configured by ``settings``; :meth:`reset` returns to them.

        * ``real``: the base clock.
        * ``frozen``: frozen at ``at`` (default: the base clock's current time).
        * ``offset``: ``offset_seconds`` ahead of the base clock or, when ``at`` is given, starting
          at ``at`` and flowing from there (like :meth:`travel_to`).
        * ``scaled``: flowing ``rate`` times as fast, starting at ``at`` (default: now).

        Settings a mode does not use are ignored.

        Raises:
            ValueError: ``offset_seconds`` or ``rate`` is not finite.
        """
        clock = cls(base)
        clock.apply(settings)
        clock._settings = settings
        return clock

    @property
    def base(self) -> Clock:
        """The clock this one derives its time from."""
        return self._base

    @property
    def mode(self) -> ClockMode:
        """The current mode."""
        return self._timeline.mode

    @property
    def rate(self) -> float:
        """Clock seconds per base-clock second (0 when frozen)."""
        return self._timeline.effective_rate

    def now_ns(self) -> int:
        """The clock's current time."""
        return self._timeline.at(self._base.now_ns())

    # -- operations ----------------------------------------------------------------------------

    def freeze(self, at_ns: int | None = None) -> None:
        """Stop the clock at ``at_ns`` (default: its current time)."""
        with self._lock:
            instant = self._timeline.at(self._base.now_ns()) if at_ns is None else at_ns
            self._timeline = _Timeline(mode="frozen", frozen_ns=instant)

    def advance(self, seconds: float) -> None:
        """Move the clock by ``seconds`` (negative values move it back), keeping its mode.

        A real-time clock becomes an ``offset`` clock running ``seconds`` ahead.

        Raises:
            ValueError: ``seconds`` is not finite.
        """
        delta = seconds_to_ns(seconds)
        with self._lock:
            timeline = self._timeline
            if timeline.mode == "frozen":
                timeline = replace(timeline, frozen_ns=timeline.frozen_ns + delta)
            elif timeline.mode == "offset":
                timeline = replace(timeline, offset_ns=timeline.offset_ns + delta)
            elif timeline.mode == "scaled":
                anchor = timeline.anchor_simulated_ns + delta
                timeline = replace(timeline, anchor_simulated_ns=anchor)
            else:
                timeline = _Timeline(mode="offset", offset_ns=delta)
            self._timeline = timeline

    def set_offset(self, seconds: float) -> None:
        """Run ``seconds`` ahead of real time (negative: behind), flowing at real speed.

        Raises:
            ValueError: ``seconds`` is not finite.
        """
        offset = seconds_to_ns(seconds)
        with self._lock:
            self._timeline = _Timeline(mode="offset", offset_ns=offset)

    def travel_to(self, at_ns: int) -> None:
        """Jump to ``at_ns`` and keep flowing at real speed from there (``offset`` mode)."""
        with self._lock:
            self._timeline = _Timeline(mode="offset", offset_ns=at_ns - self._base.now_ns())

    def scale(self, rate: float, at_ns: int | None = None) -> None:
        """Flow ``rate`` times as fast as real time, from ``at_ns`` (default: the current time).

        Raises:
            ValueError: ``rate`` is not a finite number greater than zero.
        """
        rate = _check_rate(rate)
        with self._lock:
            base_ns = self._base.now_ns()
            anchor = self._timeline.at(base_ns) if at_ns is None else at_ns
            self._timeline = _Timeline(
                mode="scaled", anchor_real_ns=base_ns, anchor_simulated_ns=anchor, rate=rate
            )

    def resume_real(self) -> None:
        """Go back to real time (the base clock)."""
        with self._lock:
            self._timeline = _Timeline()

    def apply(self, settings: ClockSettings) -> None:
        """Reconfigure the clock from ``settings``, as :meth:`from_settings` would.

        Raises:
            ValueError: ``offset_seconds`` or ``rate`` is not finite.
        """
        with self._lock:
            self._timeline = _timeline_for(settings, self._base.now_ns())

    def reset(self) -> None:
        """Return to the configuration the clock was created with.

        A clock created by :meth:`from_settings` re-applies those settings, exactly as if it had
        just been created (instants defaulting to "now" are taken afresh); any other clock goes
        back to real time.
        """
        if self._settings is None:
            self.resume_real()
        else:
            self.apply(self._settings)

    # -- inspection ----------------------------------------------------------------------------

    def state(self) -> ClockState:
        """A serializable snapshot of the clock."""
        timeline = self._timeline
        base_ns = self._base.now_ns()
        now_ns = timeline.at(base_ns)
        frozen = timeline.mode == "frozen"
        scaled = timeline.mode == "scaled"
        return ClockState(
            mode=timeline.mode,
            now=ns_to_datetime(now_ns),
            offset_seconds=(now_ns - base_ns) / NS_PER_SECOND,
            rate=timeline.effective_rate,
            frozen_at=ns_to_datetime(timeline.frozen_ns) if frozen else None,
            anchor_real=ns_to_datetime(timeline.anchor_real_ns) if scaled else None,
            anchor_simulated=ns_to_datetime(timeline.anchor_simulated_ns) if scaled else None,
        )

    def copy(self) -> ControlledClock:
        """An independent copy in the same state, sharing the base clock."""
        clone = ControlledClock(self._base)
        clone._timeline = self._timeline
        clone._settings = self._settings
        return clone


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class ClockRegistry:
    """Per-session clocks, so one evaluation's time travel does not leak into another's.

    Sessions without an override share the default clock. :meth:`override` gives a session its own
    copy, starting from the default clock's current state, that it can freeze, shift or scale.

    Args:
        default: The clock shared by every session without an override.
    """

    def __init__(self, default: ControlledClock) -> None:
        self._default = default
        self._overrides: dict[str, ControlledClock] = {}
        self._lock = threading.Lock()

    @property
    def default(self) -> ControlledClock:
        """The clock shared by sessions without an override."""
        return self._default

    def for_session(self, session_id: str | None) -> ControlledClock:
        """The clock ``session_id`` sees: its own if it has one, else the default clock."""
        if session_id is None:
            return self._default
        return self._overrides.get(session_id, self._default)

    def override(self, session_id: str) -> ControlledClock:
        """The session's own clock, copied from the default clock on first use.

        Raises:
            ValueError: ``session_id`` is empty.
        """
        if not session_id:
            raise ValueError("a session id is required to override the clock")
        with self._lock:
            clock = self._overrides.get(session_id)
            if clock is None:
                clock = self._overrides[session_id] = self._default.copy()
            return clock

    def reset(self, session_id: str | None = None) -> None:
        """Drop a session's own clock; with ``None``, drop them all and reset the default clock.

        The default clock returns to the settings it was created with (see
        :meth:`ControlledClock.reset`). Resetting a session without an override does nothing.
        """
        with self._lock:
            if session_id is not None:
                self._overrides.pop(session_id, None)
                return
            self._overrides.clear()
            self._default.reset()

    def sessions(self) -> list[str]:
        """The sessions that have their own clock, sorted."""
        with self._lock:
            return sorted(self._overrides)
