"""Per-key fixed-window rate limiting.

A key's window starts with the first request after the previous window expired and lasts
``window_seconds``; within it at most ``limit`` requests are allowed. Rejected requests do not
count. See ``GROUND_TRUTH.md`` for the exact header arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fauxcall_reference_api.clock import NS_PER_SECOND

LIMIT_HEADER: Final = "X-RateLimit-Limit"
REMAINING_HEADER: Final = "X-RateLimit-Remaining"
RESET_HEADER: Final = "X-RateLimit-Reset"
RETRY_AFTER_HEADER: Final = "Retry-After"


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


@dataclass(frozen=True, slots=True)
class RateDecision:
    """The outcome of one request against a key's window."""

    allowed: bool
    limit: int
    remaining: int
    reset_epoch_s: int
    retry_after_s: int

    def headers(self) -> dict[str, str]:
        """The rate-limit headers for the response (plus ``Retry-After`` when rejected)."""
        headers = {
            LIMIT_HEADER: str(self.limit),
            REMAINING_HEADER: str(self.remaining),
            RESET_HEADER: str(self.reset_epoch_s),
        }
        if not self.allowed:
            headers[RETRY_AFTER_HEADER] = str(self.retry_after_s)
        return headers


@dataclass(slots=True)
class _Window:
    start_ns: int
    count: int


class RateLimiter:
    """Fixed windows per key; the window starts at the first request in it."""

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_ns = window_seconds * NS_PER_SECOND
        self._windows: dict[str, _Window] = {}

    def hit(self, key: str, now_ns: int) -> RateDecision:
        """Count a request by ``key`` at ``now_ns`` (unless it is rejected)."""
        window = self._windows.get(key)
        if window is None or now_ns >= window.start_ns + self.window_ns:
            window = self._windows[key] = _Window(start_ns=now_ns, count=0)
        end_ns = window.start_ns + self.window_ns
        allowed = window.count < self.limit
        if allowed:
            window.count += 1
        return RateDecision(
            allowed=allowed,
            limit=self.limit,
            remaining=self.limit - window.count,
            reset_epoch_s=_ceil_div(end_ns, NS_PER_SECOND),
            retry_after_s=max(1, _ceil_div(end_ns - now_ns, NS_PER_SECOND)),
        )
