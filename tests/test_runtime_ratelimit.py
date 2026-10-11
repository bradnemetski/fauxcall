from __future__ import annotations

import math
from typing import Literal

import pytest

from fauxcall.model.realism import RateLimitModel
from fauxcall.runtime.ratelimit import NS, LimitStatus, RateLimiter

Algorithm = Literal["fixed_window", "sliding_window", "token_bucket"]


class Clock:
    """An injected monotonic clock (nanoseconds)."""

    def __init__(self, start_ns: int = 0) -> None:
        self.now = start_ns

    def __call__(self) -> int:
        return self.now

    def at(self, seconds: float) -> None:
        self.now = round(seconds * NS)


def model(
    limit: int, window: float, algorithm: Algorithm = "fixed_window", name: str = "api"
) -> RateLimitModel:
    return RateLimitModel(name=name, limit=limit, window_seconds=window, algorithm=algorithm)


def test_fixed_window_matches_the_documented_arithmetic() -> None:
    # The reference API's example: first request at t = 1000.3 s, 30 requests per 10 s.
    clock = Clock()
    limiter = RateLimiter(clock)
    limit = model(30, 10)
    clock.at(1000.3)
    first = limiter.hit(limit, "key")
    assert (first.allowed, first.remaining) == (True, 29)
    assert first.header_values(clock.now) == {
        "limit": 30,
        "remaining": 29,
        "reset_seconds": 10,
        "reset_epoch": 1011,
        "window_seconds": 10,
    }
    for expected in range(28, -1, -1):
        assert limiter.hit(limit, "key").remaining == expected
    clock.at(1004.8)
    blocked = limiter.hit(limit, "key")
    assert (blocked.allowed, blocked.remaining, blocked.retry_after) == (False, 0, 6)
    assert blocked.header_values(clock.now)["reset_epoch"] == 1011
    clock.at(1005.3)
    assert limiter.hit(limit, "key").retry_after == 5
    clock.at(1010.3)  # the end instant opens a new window; rejected requests didn't extend it
    reopened = limiter.hit(limit, "key")
    assert (reopened.allowed, reopened.remaining) == (True, 29)
    assert reopened.header_values(clock.now)["reset_epoch"] == 1021


def test_fixed_window_blocks_until_the_end_instant() -> None:
    clock = Clock()
    limiter = RateLimiter(clock)
    limit = model(2, 1)
    assert [limiter.hit(limit, "k").allowed for _ in range(3)] == [True, True, False]
    clock.now = NS - 1
    last_moment = limiter.hit(limit, "k")
    assert not last_moment.allowed
    assert last_moment.reset_after_ns == 1
    assert last_moment.retry_after == 1
    clock.now = NS
    assert limiter.hit(limit, "k").allowed


def test_sliding_window() -> None:
    clock = Clock()
    limiter = RateLimiter(clock)
    limit = model(2, 10, "sliding_window")
    first = limiter.hit(limit, "k")
    assert (first.allowed, first.remaining, first.reset_after_s) == (True, 1, 10.0)
    clock.at(4)
    second = limiter.hit(limit, "k")
    assert (second.allowed, second.remaining, second.reset_after_s) == (True, 0, 6.0)
    clock.at(5)
    blocked = limiter.hit(limit, "k")
    assert (blocked.allowed, blocked.remaining, blocked.retry_after) == (False, 0, 5)
    clock.at(10)  # the hit at t=0 has left the window
    third = limiter.hit(limit, "k")
    assert (third.allowed, third.remaining, third.reset_after_s) == (True, 0, 4.0)
    clock.at(13.9)
    nearly = limiter.hit(limit, "k")
    assert not nearly.allowed
    assert nearly.retry_after == 1
    assert nearly.header_values(clock.now)["reset_seconds"] == 1


def test_token_bucket_refills_continuously_and_exactly() -> None:
    clock = Clock()
    limiter = RateLimiter(clock)
    limit = model(3, 3600, "token_bucket")
    hits = [limiter.hit(limit, "k") for _ in range(3)]
    assert [h.remaining for h in hits] == [2, 1, 0]
    assert hits[0].reset_after_s == 1200.0  # time until the bucket is full again
    blocked = limiter.hit(limit, "k")
    assert (blocked.allowed, blocked.retry_after) == (False, 1200)
    clock.at(1200)  # waiting exactly Retry-After is enough
    assert limiter.hit(limit, "k").allowed
    assert not limiter.hit(limit, "k").allowed
    clock.at(1200 + 36_000)  # a long idle refills to capacity, not beyond
    assert limiter.hit(limit, "k").remaining == 2


@pytest.mark.parametrize(("limit", "window"), [(25, 0.7), (3, 3600), (36, 3600), (7, 3), (1, 1)])
def test_token_bucket_has_no_rounding_errors(limit: int, window: float) -> None:
    clock = Clock()
    limiter = RateLimiter(clock)
    bucket = model(limit, window, "token_bucket")
    for _ in range(limit):
        assert limiter.hit(bucket, "k").allowed
    blocked = limiter.hit(bucket, "k")
    assert not blocked.allowed
    clock.now = blocked.reset_after_ns
    assert limiter.hit(bucket, "k").allowed
    assert blocked.reset_after_ns == math.ceil(round(window * NS) / limit)


def test_keys_limits_and_reset_are_independent() -> None:
    clock = Clock()
    limiter = RateLimiter(clock)
    small = model(1, 60)
    other = model(1, 60, name="other")
    assert limiter.hit(small, "alice").allowed
    assert not limiter.hit(small, "alice").allowed
    assert limiter.hit(small, "bob").allowed
    assert limiter.hit(other, "alice").allowed
    limiter.reset()
    assert limiter.hit(small, "alice").allowed


def test_tiny_windows_do_not_divide_by_zero() -> None:
    clock = Clock()
    limiter = RateLimiter(clock)
    for algorithm in ("fixed_window", "sliding_window", "token_bucket"):
        limit = model(1, 1e-12, algorithm, name=algorithm)
        assert limiter.hit(limit, "k").allowed
        assert not limiter.hit(limit, "k").allowed
        clock.now += 1
        assert limiter.hit(limit, "k").allowed


def test_header_values_and_retry_after_edges() -> None:
    limit = model(5, 0.5)
    status = LimitStatus(limit, allowed=True, remaining=4, reset_after_ns=-3)
    assert status.header_values(10 * NS + 1) == {
        "limit": 5,
        "remaining": 4,
        "reset_seconds": 0,
        "reset_epoch": 11,
        "window_seconds": 0.5,
    }
    assert status.retry_after == 1
    assert (
        LimitStatus(limit, allowed=False, remaining=0, reset_after_ns=2 * NS + 1).retry_after == 3
    )


def test_default_clock_is_monotonic() -> None:
    limiter = RateLimiter()
    status = limiter.hit(model(10, 60), "k")
    assert status.allowed
    assert 0 < status.reset_after_ns <= 60 * NS
