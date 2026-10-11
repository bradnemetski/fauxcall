"""Injectable time.

Everything that depends on time reads a :class:`Clock` (integer nanoseconds since the Unix epoch)
and waits through a :data:`Sleep` coroutine function, so tests can freeze, move and fast-forward
time. The server uses :class:`SystemClock` and :func:`real_sleep`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

import anyio
import anyio.lowlevel

NS_PER_SECOND: Final = 1_000_000_000
NS_PER_MS: Final = 1_000_000

type Sleep = Callable[[float], Awaitable[None]]
"""An awaitable ``sleep(seconds)``."""


class Clock(Protocol):
    """Anything that can tell the current time."""

    def now_ns(self) -> int:
        """Return the current instant as integer nanoseconds since the Unix epoch."""
        ...


class SystemClock:
    """The real wall clock."""

    def now_ns(self) -> int:
        """Return :func:`time.time_ns`."""
        return time.time_ns()


class ManualClock:
    """A clock that only moves when told to.

    Its :meth:`sleep` advances the clock by the requested duration instead of waiting, and records
    the duration in :attr:`sleeps`, so streaming code paced by sleeps runs instantly in tests while
    observing exactly the timestamps it would have produced in real time.
    """

    def __init__(self, start_ns: int) -> None:
        self._now_ns = start_ns
        self.sleeps: list[float] = []

    def now_ns(self) -> int:
        """Return the current (virtual) instant."""
        return self._now_ns

    def set_ns(self, instant_ns: int) -> None:
        """Jump to an absolute instant."""
        self._now_ns = instant_ns

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds`` (rounded to the nearest nanosecond)."""
        self._now_ns += round(seconds * NS_PER_SECOND)

    async def sleep(self, seconds: float) -> None:
        """Advance the clock by ``seconds`` without waiting (but still yield to the event loop)."""
        self.sleeps.append(seconds)
        self.advance(seconds)
        await anyio.lowlevel.checkpoint()


async def real_sleep(seconds: float) -> None:
    """Wait for ``seconds`` of real time."""
    await anyio.sleep(seconds)
