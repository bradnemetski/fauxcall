"""Rate-limit emulation matching the real API's limits, headers and 429 responses.

Windows are measured in *real* elapsed time (so a frozen evaluation clock can't lock an agent out
forever), while reset timestamps in headers are rendered on the fake's clock. All arithmetic is in
integer nanoseconds, so a client that waits exactly ``Retry-After`` seconds is let through.

Algorithms:

* ``fixed_window``: a window starts with the first request after the previous one ended and lasts
  ``window_seconds``; a request at or after the end instant opens a new window.
* ``sliding_window``: at most ``limit`` requests in any ``window_seconds`` (a log of hits).
* ``token_bucket``: a bucket of ``limit`` tokens refilled continuously at ``limit`` per window.

Rejected requests are never counted.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from fauxcall.model.realism import RateLimitModel

NS = 1_000_000_000


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


@dataclass(slots=True)
class _Window:
    start_ns: int = 0
    count: int = 0
    hits: deque[int] = field(default_factory=deque)
    level: int | None = None  # token bucket: tokens x window_ns (exact integer arithmetic)
    refreshed_ns: int = 0


@dataclass(frozen=True, slots=True)
class LimitStatus:
    """Outcome of counting one request against one limit."""

    limit: RateLimitModel
    allowed: bool
    remaining: int
    reset_after_ns: int = 0

    @property
    def reset_after_s(self) -> float:
        """Seconds until the window resets (blocked: until a request would be allowed again)."""
        return self.reset_after_ns / NS

    def header_values(self, now_ns: int) -> dict[str, int | float]:
        """Values for ``{{limit}}``, ``{{remaining}}``, ``{{reset_epoch}}`` … placeholders.

        ``reset_epoch`` is the reset instant in epoch seconds on the fake clock, rounded up;
        ``reset_seconds`` the whole seconds until then (rounded up).
        """
        reset_after = max(0, self.reset_after_ns)
        window = self.limit.window_seconds
        return {
            "limit": self.limit.limit,
            "remaining": self.remaining,
            "reset_seconds": _ceil_div(reset_after, NS),
            "reset_epoch": _ceil_div(now_ns + reset_after, NS),
            "window_seconds": int(window) if float(window).is_integer() else window,
        }

    @property
    def retry_after(self) -> int:
        """Whole seconds to wait (at least 1) when blocked."""
        return max(1, _ceil_div(self.reset_after_ns, NS))


class RateLimiter:
    """Counts requests per limit and key."""

    def __init__(self, monotonic_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._now = monotonic_ns
        self._windows: defaultdict[tuple[str, str], _Window] = defaultdict(_Window)

    def reset(self) -> None:
        """Forget every window."""
        self._windows.clear()

    def hit(self, limit: RateLimitModel, key: str) -> LimitStatus:
        """Count one request; returns whether it is allowed and the remaining budget."""
        now = self._now()
        window_ns = max(1, round(limit.window_seconds * NS))
        state = self._windows[limit.name, key]
        match limit.algorithm:
            case "sliding_window":
                return self._sliding(limit, state, now, window_ns)
            case "token_bucket":
                return self._bucket(limit, state, now, window_ns)
            case _:
                return self._fixed(limit, state, now, window_ns)

    @staticmethod
    def _fixed(limit: RateLimitModel, state: _Window, now: int, window_ns: int) -> LimitStatus:
        if state.count == 0 or now - state.start_ns >= window_ns:
            state.start_ns, state.count = now, 0
        reset_after = state.start_ns + window_ns - now
        if state.count >= limit.limit:
            return LimitStatus(limit, allowed=False, remaining=0, reset_after_ns=reset_after)
        state.count += 1
        return LimitStatus(
            limit, allowed=True, remaining=limit.limit - state.count, reset_after_ns=reset_after
        )

    @staticmethod
    def _sliding(limit: RateLimitModel, state: _Window, now: int, window_ns: int) -> LimitStatus:
        while state.hits and now - state.hits[0] >= window_ns:
            state.hits.popleft()
        if len(state.hits) >= limit.limit:
            reset_after = state.hits[0] + window_ns - now
            return LimitStatus(limit, allowed=False, remaining=0, reset_after_ns=reset_after)
        state.hits.append(now)
        return LimitStatus(
            limit,
            allowed=True,
            remaining=limit.limit - len(state.hits),
            reset_after_ns=state.hits[0] + window_ns - now,
        )

    @staticmethod
    def _bucket(limit: RateLimitModel, state: _Window, now: int, window_ns: int) -> LimitStatus:
        # Levels are counted in token x window_ns units: the bucket gains `limit` units per
        # nanosecond and a request costs `window_ns` units, so nothing is ever rounded.
        capacity = limit.limit * window_ns
        if state.level is None:
            state.level, state.refreshed_ns = capacity, now
        elapsed = max(0, now - state.refreshed_ns)
        state.level = min(capacity, state.level + elapsed * limit.limit)
        state.refreshed_ns = now
        if state.level < window_ns:
            wait = _ceil_div(window_ns - state.level, limit.limit)
            return LimitStatus(limit, allowed=False, remaining=0, reset_after_ns=wait)
        state.level -= window_ns
        return LimitStatus(
            limit,
            allowed=True,
            remaining=state.level // window_ns,
            reset_after_ns=_ceil_div(capacity - state.level, limit.limit),
        )
