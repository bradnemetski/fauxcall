from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from fauxcall.clock import (
    ClockRegistry,
    ClockState,
    ControlledClock,
    ManualClock,
    SystemClock,
    datetime_to_ns,
    ns_to_datetime,
    seconds_to_ns,
)
from fauxcall.config import ClockSettings

if TYPE_CHECKING:
    from collections.abc import Callable

SECOND = 1_000_000_000
START = datetime(2026, 10, 9, 13, 30, tzinfo=UTC)  # Friday, the NYSE opening bell
START_NS = 1_791_552_600 * SECOND
LATER = datetime(2027, 1, 4, 14, 30, tzinfo=UTC)
LATER_NS = datetime_to_ns(LATER)
NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def base() -> ManualClock:
    return ManualClock(START_NS)


@pytest.fixture
def clock(base: ManualClock) -> ControlledClock:
    return ControlledClock(base)


class _Floating(tzinfo):
    """A tzinfo without an offset: datetimes using it are naive."""

    def utcoffset(self, _dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, _dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, _dt: datetime | None) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def test_datetime_to_ns_is_exact() -> None:
    assert datetime_to_ns(START) == START_NS
    assert datetime_to_ns(START.replace(microsecond=123_456)) == START_NS + 123_456_000
    assert datetime_to_ns(datetime(2026, 10, 9, 9, 30, tzinfo=NEW_YORK)) == START_NS
    assert datetime_to_ns(datetime(2026, 10, 9, 15, 30, tzinfo=timezone(timedelta(hours=2)))) == (
        START_NS
    )
    assert datetime_to_ns(datetime(1970, 1, 1, tzinfo=UTC)) == 0
    assert datetime_to_ns(datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)) == -1_000


@pytest.mark.parametrize(
    "moment",
    [
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(1969, 12, 31, 23, 59, 59, 1, tzinfo=UTC),
        datetime(2026, 3, 8, 3, 0, 0, 999_999, tzinfo=NEW_YORK),
        datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NEW_YORK),
        datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC),
    ],
)
def test_datetime_round_trip_keeps_microseconds(moment: datetime) -> None:
    ns = datetime_to_ns(moment)
    assert ns % 1_000 == 0
    back = ns_to_datetime(ns, moment.tzinfo or UTC)
    assert back == moment
    assert back.fold == moment.fold  # same-zone equality ignores fold
    assert datetime_to_ns(back) == ns
    assert ns_to_datetime(ns).microsecond == moment.microsecond


def test_fold_selects_the_second_occurrence() -> None:
    first = datetime(2026, 11, 1, 1, 30, tzinfo=NEW_YORK)
    second = first.replace(fold=1)
    assert datetime_to_ns(second) - datetime_to_ns(first) == 3_600 * SECOND


def test_datetime_to_ns_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        datetime_to_ns(datetime(2026, 10, 9, 13, 30))  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="timezone-aware"):
        datetime_to_ns(datetime(2026, 10, 9, 13, 30, tzinfo=_Floating()))


def test_ns_to_datetime_truncates_towards_the_past() -> None:
    assert ns_to_datetime(START_NS + 999) == START
    assert ns_to_datetime(START_NS + 1_999).microsecond == 1
    assert ns_to_datetime(-1) == datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)
    assert ns_to_datetime(START_NS).tzinfo is UTC


def test_ns_to_datetime_in_a_zone() -> None:
    local = ns_to_datetime(START_NS, NEW_YORK)
    assert local.tzinfo is NEW_YORK
    assert (local.hour, local.minute) == (9, 30)
    assert local.utcoffset() == timedelta(hours=-4)
    winter = ns_to_datetime(datetime_to_ns(datetime(2026, 12, 1, 14, 30, tzinfo=UTC)), NEW_YORK)
    assert (winter.hour, winter.utcoffset()) == (9, timedelta(hours=-5))


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, 0),
        (1, SECOND),
        (0.1, 100_000_000),
        (0.3, 300_000_000),
        (1.5, 1_500_000_000),
        (-0.25, -250_000_000),
        (1e-9, 1),
        (4e-10, 0),
        (6e-10, 1),
        (2.5e-9, 2),  # ties round to even
        (86_400.000000001, 86_400_000_000_001),
        (31_536_000.123456787, 31_536_000_123_456_787),  # float math would say ...788
        (1e22, 10**31),
    ],
)
def test_seconds_to_ns(seconds: float, expected: int) -> None:
    assert seconds_to_ns(seconds) == expected


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), float("-inf")])
def test_seconds_to_ns_rejects_non_finite_values(seconds: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        seconds_to_ns(seconds)


# ---------------------------------------------------------------------------
# Basic clocks
# ---------------------------------------------------------------------------


def test_system_clock_reads_time_ns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time_ns", lambda: START_NS + 7)
    assert SystemClock().now_ns() == START_NS + 7


def test_manual_clock_moves_only_when_told(base: ManualClock) -> None:
    assert base.now_ns() == START_NS
    assert base.now_ns() == START_NS
    base.advance(1.5)
    assert base.now_ns() == START_NS + 1_500_000_000
    base.advance(-0.5)
    assert base.now_ns() == START_NS + SECOND
    base.set(LATER_NS)
    assert base.now_ns() == LATER_NS
    base.set(0)
    assert base.now_ns() == 0


# ---------------------------------------------------------------------------
# ControlledClock modes
# ---------------------------------------------------------------------------


def test_defaults_to_real_system_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time_ns", lambda: LATER_NS)
    clock = ControlledClock()
    assert isinstance(clock.base, SystemClock)
    assert clock.mode == "real"
    assert clock.now_ns() == LATER_NS


def test_real_mode_tracks_the_base_clock(clock: ControlledClock, base: ManualClock) -> None:
    assert clock.base is base
    assert clock.now_ns() == START_NS
    base.advance(5)
    assert clock.now_ns() == START_NS + 5 * SECOND


def test_freeze_at_the_current_time(clock: ControlledClock, base: ManualClock) -> None:
    base.advance(2)
    clock.freeze()
    base.advance(3_600)
    assert clock.mode == "frozen"
    assert clock.now_ns() == START_NS + 2 * SECOND


def test_freeze_at_an_instant_and_advance(clock: ControlledClock, base: ManualClock) -> None:
    clock.freeze(LATER_NS)
    base.advance(60)
    assert clock.now_ns() == LATER_NS
    clock.advance(90)
    assert clock.now_ns() == LATER_NS + 90 * SECOND
    clock.advance(-0.5)
    assert clock.now_ns() == LATER_NS + 89_500_000_000
    assert clock.mode == "frozen"


def test_offset_keeps_flowing(clock: ControlledClock, base: ManualClock) -> None:
    clock.set_offset(-3_600)
    assert clock.mode == "offset"
    assert clock.now_ns() == START_NS - 3_600 * SECOND
    base.advance(10)
    assert clock.now_ns() == START_NS - 3_590 * SECOND
    clock.advance(60)
    assert clock.now_ns() == START_NS - 3_530 * SECOND
    clock.set_offset(0.25)
    assert clock.now_ns() == START_NS + 10 * SECOND + 250_000_000


def test_advance_in_real_mode_switches_to_offset(clock: ControlledClock, base: ManualClock) -> None:
    clock.advance(86_400)
    assert clock.mode == "offset"
    assert clock.now_ns() == START_NS + 86_400 * SECOND
    base.advance(1)
    assert clock.now_ns() == START_NS + 86_401 * SECOND


def test_travel_to_jumps_and_keeps_flowing(clock: ControlledClock, base: ManualClock) -> None:
    clock.freeze(START_NS - 3 * SECOND)
    clock.travel_to(LATER_NS)
    assert clock.mode == "offset"
    assert clock.now_ns() == LATER_NS
    base.advance(10)
    assert clock.now_ns() == LATER_NS + 10 * SECOND
    clock.travel_to(START_NS)  # backwards too
    assert clock.now_ns() == START_NS


def test_scale_accelerates_from_the_current_time(clock: ControlledClock, base: ManualClock) -> None:
    clock.set_offset(3_600)
    base.advance(1)
    clock.scale(60)
    assert clock.mode == "scaled"
    assert clock.now_ns() == START_NS + 3_601 * SECOND  # continuous at the switch
    base.advance(1)
    assert clock.now_ns() == START_NS + 3_661 * SECOND
    base.advance(0.5)
    assert clock.now_ns() == START_NS + 3_691 * SECOND


def test_scale_from_an_instant(clock: ControlledClock, base: ManualClock) -> None:
    clock.scale(2, at_ns=LATER_NS)
    assert clock.now_ns() == LATER_NS
    base.advance(0.5)
    assert clock.now_ns() == LATER_NS + SECOND
    base.set(START_NS - SECOND)  # the base going backwards is mirrored, scaled
    assert clock.now_ns() == LATER_NS - 2 * SECOND


def test_slow_motion_uses_exact_arithmetic(clock: ControlledClock, base: ManualClock) -> None:
    clock.scale(0.1)
    base.advance(10)
    assert clock.now_ns() == START_NS + SECOND
    base.advance(3e-9)  # 0.3 ns of simulated time: not a whole nanosecond yet
    assert clock.now_ns() == START_NS + SECOND


def test_rescaling_and_advancing_keep_time_continuous(
    clock: ControlledClock, base: ManualClock
) -> None:
    clock.scale(10)
    base.advance(1)
    assert clock.now_ns() == START_NS + 10 * SECOND
    clock.scale(100)
    assert clock.now_ns() == START_NS + 10 * SECOND
    base.advance(1)
    assert clock.now_ns() == START_NS + 110 * SECOND
    clock.advance(-110)
    assert clock.now_ns() == START_NS
    base.advance(0.01)
    assert clock.now_ns() == START_NS + SECOND
    clock.freeze()
    base.advance(1)
    assert clock.now_ns() == START_NS + SECOND


def test_resume_real_from_every_mode(clock: ControlledClock, base: ManualClock) -> None:
    changes: list[Callable[[], None]] = [
        lambda: clock.freeze(LATER_NS),
        lambda: clock.set_offset(-60),
        lambda: clock.scale(5, at_ns=LATER_NS),
    ]
    for change in changes:
        change()
        base.advance(1)
        clock.resume_real()
        assert clock.mode == "real"
        assert clock.now_ns() == base.now_ns()


@pytest.mark.parametrize("rate", [0.0, -1.0, float("nan"), float("inf")])
def test_scale_rejects_invalid_rates(clock: ControlledClock, rate: float) -> None:
    clock.set_offset(30)
    with pytest.raises(ValueError, match="rate"):
        clock.scale(rate)
    assert clock.mode == "offset"
    assert clock.now_ns() == START_NS + 30 * SECOND


def test_non_finite_seconds_are_rejected(clock: ControlledClock) -> None:
    with pytest.raises(ValueError, match="finite"):
        clock.advance(float("nan"))
    with pytest.raises(ValueError, match="finite"):
        clock.set_offset(float("inf"))
    assert clock.mode == "real"


def test_concurrent_writers_never_lose_an_update(clock: ControlledClock) -> None:
    clock.freeze(START_NS)

    def tick() -> None:
        for _ in range(500):
            clock.advance(1e-9)

    workers = [threading.Thread(target=tick) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert clock.now_ns() == START_NS + 2_000


# ---------------------------------------------------------------------------
# State, copies, settings
# ---------------------------------------------------------------------------


def test_state_in_real_mode(clock: ControlledClock) -> None:
    assert clock.state() == ClockState(mode="real", now=START)


def test_state_in_frozen_mode(clock: ControlledClock, base: ManualClock) -> None:
    clock.freeze()
    base.advance(30)
    state = clock.state()
    assert state == ClockState(
        mode="frozen", now=START, offset_seconds=-30.0, rate=0.0, frozen_at=START
    )
    assert state.model_dump(mode="json") == {
        "mode": "frozen",
        "now": "2026-10-09T13:30:00Z",
        "offset_seconds": -30.0,
        "rate": 0.0,
        "frozen_at": "2026-10-09T13:30:00Z",
        "anchor_real": None,
        "anchor_simulated": None,
    }


def test_state_in_offset_mode(clock: ControlledClock) -> None:
    clock.set_offset(90.5)
    assert clock.state() == ClockState(
        mode="offset", now=START + timedelta(seconds=90.5), offset_seconds=90.5
    )


def test_state_in_scaled_mode(clock: ControlledClock, base: ManualClock) -> None:
    clock.scale(60, at_ns=LATER_NS)
    base.advance(2)
    state = clock.state()
    assert state.mode == "scaled"
    assert state.now == LATER + timedelta(minutes=2)
    assert state.rate == 60.0
    assert state.offset_seconds == (LATER_NS + 120 * SECOND - base.now_ns()) / SECOND
    assert state.anchor_real == START
    assert state.anchor_simulated == LATER
    assert state.frozen_at is None


def test_clock_state_is_strict() -> None:
    with pytest.raises(ValidationError):
        ClockState.model_validate({"mode": "real", "now": "2026-10-09T13:30:00"})
    with pytest.raises(ValidationError):
        ClockState.model_validate({"mode": "warp", "now": "2026-10-09T13:30:00Z"})
    with pytest.raises(ValidationError):
        ClockState.model_validate({"mode": "real", "now": "2026-10-09T13:30:00Z", "speed": 2})


def test_copies_are_independent(clock: ControlledClock, base: ManualClock) -> None:
    clock.set_offset(60)
    twin = clock.copy()
    assert twin is not clock
    assert twin.base is base
    assert twin.state() == clock.state()
    twin.freeze()
    base.advance(1)
    assert clock.now_ns() == START_NS + 61 * SECOND
    assert twin.now_ns() == START_NS + 60 * SECOND
    clock.resume_real()
    assert twin.mode == "frozen"


def test_from_settings_real_ignores_unused_settings(base: ManualClock) -> None:
    clock = ControlledClock.from_settings(
        ClockSettings(mode="real", at=LATER, offset_seconds=60, rate=5), base
    )
    assert clock.mode == "real"
    assert clock.now_ns() == START_NS


def test_from_settings_defaults_to_the_system_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time_ns", lambda: START_NS)
    clock = ControlledClock.from_settings(ClockSettings(mode="frozen"))
    assert isinstance(clock.base, SystemClock)
    assert clock.now_ns() == START_NS


def test_from_settings_frozen(base: ManualClock) -> None:
    at = ControlledClock.from_settings(ClockSettings(mode="frozen", at=LATER), base)
    now = ControlledClock.from_settings(ClockSettings(mode="frozen"), base)
    base.advance(3_600)
    assert at.now_ns() == LATER_NS
    assert now.now_ns() == START_NS
    assert at.mode == now.mode == "frozen"


def test_from_settings_offset(base: ManualClock) -> None:
    shifted = ControlledClock.from_settings(
        ClockSettings(mode="offset", offset_seconds=-86_400), base
    )
    started = ControlledClock.from_settings(
        ClockSettings(mode="offset", at=LATER, offset_seconds=5), base
    )
    assert shifted.now_ns() == START_NS - 86_400 * SECOND
    assert started.now_ns() == LATER_NS  # `at` wins: start there, flow at real speed
    base.advance(10)
    assert shifted.now_ns() == START_NS - 86_390 * SECOND
    assert started.now_ns() == LATER_NS + 10 * SECOND
    assert shifted.mode == started.mode == "offset"


def test_from_settings_scaled(base: ManualClock) -> None:
    at = ControlledClock.from_settings(ClockSettings(mode="scaled", at=LATER, rate=60), base)
    now = ControlledClock.from_settings(ClockSettings(mode="scaled", rate=0.5), base)
    assert at.now_ns() == LATER_NS
    assert now.now_ns() == START_NS
    base.advance(2)
    assert at.now_ns() == LATER_NS + 120 * SECOND
    assert now.now_ns() == START_NS + SECOND


@pytest.mark.parametrize(
    "settings",
    [
        # model_construct bypasses validation, like a hand-built or legacy settings object would.
        ClockSettings.model_construct(mode="offset", offset_seconds=float("inf")),
        ClockSettings.model_construct(mode="scaled", rate=float("inf")),
    ],
)
def test_from_settings_rejects_non_finite_values(
    settings: ClockSettings, base: ManualClock
) -> None:
    with pytest.raises(ValueError, match="finite"):
        ControlledClock.from_settings(settings, base)


@pytest.mark.parametrize("field", ["offset_seconds", "rate"])
def test_settings_reject_non_finite_values(field: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        ClockSettings.model_validate({field: float("inf")})


def test_apply_reconfigures_in_place(clock: ControlledClock, base: ManualClock) -> None:
    clock.set_offset(-60)
    clock.apply(ClockSettings(mode="frozen"))
    base.advance(5)
    assert clock.now_ns() == START_NS  # "now" means real time, not the clock's shifted time
    clock.apply(ClockSettings(mode="scaled", at=LATER, rate=2))
    base.advance(1)
    assert clock.now_ns() == LATER_NS + 2 * SECOND


def test_reset_without_settings_returns_to_real_time(
    clock: ControlledClock, base: ManualClock
) -> None:
    clock.scale(3, at_ns=LATER_NS)
    base.advance(1)
    clock.reset()
    assert clock.mode == "real"
    assert clock.now_ns() == base.now_ns()


def test_reset_reapplies_the_initial_settings(base: ManualClock) -> None:
    replay = ControlledClock.from_settings(ClockSettings(mode="scaled", at=LATER, rate=60), base)
    frozen_now = ControlledClock.from_settings(ClockSettings(mode="frozen"), base)
    replay.apply(ClockSettings(mode="real"))
    base.advance(10)
    frozen_now.advance(3_600)
    replay.reset()
    frozen_now.reset()
    assert replay.mode == "scaled"
    assert replay.now_ns() == LATER_NS  # the scenario starts over
    assert frozen_now.now_ns() == START_NS + 10 * SECOND  # frozen at the (new) real time
    twin = replay.copy()
    twin.resume_real()
    twin.reset()
    assert twin.mode == "scaled"
    assert twin.now_ns() == LATER_NS


# ---------------------------------------------------------------------------
# ClockRegistry
# ---------------------------------------------------------------------------


def test_sessions_share_the_default_until_overridden(clock: ControlledClock) -> None:
    registry = ClockRegistry(clock)
    assert registry.default is clock
    assert registry.for_session(None) is clock
    assert registry.for_session("eval-1") is clock
    assert registry.for_session("") is clock
    assert registry.sessions() == []


def test_overrides_are_isolated(clock: ControlledClock, base: ManualClock) -> None:
    registry = ClockRegistry(clock)
    clock.set_offset(3_600)
    own = registry.override("eval-1")
    assert own is not clock
    assert registry.override("eval-1") is own
    assert registry.for_session("eval-1") is own
    assert own.now_ns() == START_NS + 3_600 * SECOND  # starts from the default's state
    own.freeze(LATER_NS)
    clock.resume_real()
    base.advance(1)
    assert own.now_ns() == LATER_NS
    assert registry.for_session("eval-2").now_ns() == START_NS + SECOND
    assert registry.for_session(None) is clock


def test_override_requires_a_session_id(clock: ControlledClock) -> None:
    with pytest.raises(ValueError, match="session id"):
        ClockRegistry(clock).override("")


def test_reset_one_session(clock: ControlledClock) -> None:
    registry = ClockRegistry(clock)
    registry.override("b").freeze(LATER_NS)
    registry.override("a").set_offset(5)
    assert registry.sessions() == ["a", "b"]
    registry.reset("b")
    registry.reset("unknown")
    assert registry.sessions() == ["a"]
    assert registry.for_session("b") is clock
    assert registry.override("b").now_ns() == START_NS  # a fresh copy of the default


def test_reset_everything(base: ManualClock) -> None:
    default = ControlledClock.from_settings(ClockSettings(mode="frozen", at=LATER), base)
    registry = ClockRegistry(default)
    registry.override("eval-1").travel_to(START_NS)
    default.scale(100)
    base.advance(1)
    registry.reset()
    assert registry.sessions() == []
    assert registry.default is default
    assert registry.for_session("eval-1") is default
    assert default.state() == ClockState(
        mode="frozen",
        now=LATER,
        offset_seconds=(LATER_NS - base.now_ns()) / SECOND,
        rate=0.0,
        frozen_at=LATER,
    )
