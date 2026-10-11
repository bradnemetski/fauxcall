from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from fauxcall_reference_api.clock import ManualClock, SystemClock, real_sleep
from fauxcall_reference_api.timefmt import from_datetime, iso_python, iso_z, to_datetime

from .helpers import SECOND, at

# 2026-10-11T01:17:26.844021678Z
INSTANT = at(2026, 10, 11, 1, 17, 26) + 844_021_678


@pytest.mark.parametrize(
    ("digits", "expected"),
    [
        (0, "2026-10-11T01:17:26Z"),
        (3, "2026-10-11T01:17:26.844Z"),
        (6, "2026-10-11T01:17:26.844021Z"),
        (7, "2026-10-11T01:17:26.8440216Z"),
        (9, "2026-10-11T01:17:26.844021678Z"),
    ],
)
def test_iso_z_truncates_to_the_requested_digits(digits: int, expected: str) -> None:
    assert iso_z(INSTANT, digits) == expected


def test_iso_z_pads_small_fractions_and_rejects_bad_digit_counts() -> None:
    assert iso_z(at(2026, 1, 2) + 5_000, 7) == "2026-01-02T00:00:00.0000050Z"
    with pytest.raises(ValueError, match="between 0 and 9"):
        iso_z(INSTANT, 10)


def test_iso_python_matches_datetime_isoformat() -> None:
    assert iso_python(INSTANT) == "2026-10-11T01:17:26.844021+00:00"
    assert iso_python(at(2026, 10, 11, 1, 17, 26)) == "2026-10-11T01:17:26+00:00"


def test_datetime_round_trip() -> None:
    moment = datetime(2026, 10, 11, 1, 17, 26, 844021, tzinfo=UTC)
    assert to_datetime(from_datetime(moment)) == moment
    assert to_datetime(INSTANT) == moment  # truncated to microseconds


def test_system_clock_reads_wall_time() -> None:
    before = time.time_ns()
    assert before <= SystemClock().now_ns() <= time.time_ns()


async def test_manual_clock_moves_only_when_told() -> None:
    clock = ManualClock(INSTANT)
    assert clock.now_ns() == INSTANT
    clock.advance(1.5)
    assert clock.now_ns() == INSTANT + 1_500_000_000
    clock.set_ns(42)
    assert clock.now_ns() == 42
    await clock.sleep(0.2)
    await clock.sleep(0.25)
    assert clock.now_ns() == 42 + 450_000_000
    assert clock.sleeps == [0.2, 0.25]


async def test_real_sleep_waits() -> None:
    started = time.perf_counter()
    await real_sleep(0.01)
    assert time.perf_counter() - started >= 0.009
    assert SECOND == 1_000_000_000
