from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo

from fauxcall_reference_api.timefmt import from_datetime

DEMO: Final = {"X-API-Key": "demo-key"}
READONLY: Final = {"X-API-Key": "readonly-key"}
REVOKED: Final = {"X-API-Key": "revoked-key"}

# Friday 2026-10-09 15:00:00 UTC: New York 11:00 (open), London 16:00 (open), Tokyo 00:00 Sat.
DEFAULT_INSTANT: Final = from_datetime(datetime(2026, 10, 9, 15, 0, tzinfo=UTC))
SECOND: Final = 1_000_000_000
ERROR_KEYS: Final = frozenset({"code", "message", "request_id", "param"})


def at(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    *,
    tz: str = "UTC",
    nanos: int = 0,
) -> int:
    """Epoch nanoseconds of a wall-clock time in ``tz``."""
    moment = datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(tz))
    return from_datetime(moment) + nanos


def error_of(body: object) -> dict[str, str]:
    """The ``error`` object of an envelope, after checking the envelope's shape."""
    assert isinstance(body, dict)
    assert set(body) == {"error"}
    error = body["error"]
    assert isinstance(error, dict)
    assert {"code", "message", "request_id"} <= set(error) <= ERROR_KEYS
    assert all(isinstance(value, str) for value in error.values())
    return error
