"""Render instants (integer nanoseconds since the Unix epoch) in the exact formats the API emits.

The API deliberately uses several timestamp spellings so FauxCall's format inference is exercised:

* :func:`iso_z` with 3, 6 or 7 fractional digits, e.g. ``2026-10-09T20:00:00.000000Z``;
* :func:`iso_python`, Python's ``datetime.isoformat()``: ``2026-10-09T20:00:00.123456+00:00``
  (the fraction is omitted entirely when the microseconds are zero).

Fractions are always truncated (floored), never rounded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from fauxcall_reference_api.clock import NS_PER_SECOND

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NS_PER_MICROSECOND: Final = 1_000
_MAX_FRACTION_DIGITS: Final = 9


def to_datetime(instant_ns: int) -> datetime:
    """Convert an instant to an aware UTC datetime, truncated to microseconds."""
    return EPOCH + timedelta(microseconds=instant_ns // _NS_PER_MICROSECOND)


def from_datetime(moment: datetime) -> int:
    """Convert an aware datetime to integer nanoseconds since the epoch (exactly)."""
    return (moment - EPOCH) // timedelta(microseconds=1) * _NS_PER_MICROSECOND


def iso_z(instant_ns: int, fraction_digits: int) -> str:
    """Render ``YYYY-MM-DDTHH:MM:SS[.f...]Z`` in UTC with exactly ``fraction_digits`` digits."""
    if not 0 <= fraction_digits <= _MAX_FRACTION_DIGITS:
        raise ValueError(f"fraction_digits must be between 0 and 9, got {fraction_digits}")
    seconds, nanos = divmod(instant_ns, NS_PER_SECOND)
    stamp = (EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    if fraction_digits == 0:
        return f"{stamp}Z"
    return f"{stamp}.{nanos:09d}"[: len(stamp) + 1 + fraction_digits] + "Z"


def iso_python(instant_ns: int) -> str:
    """Render like Python's ``datetime.isoformat()`` for a UTC datetime (``+00:00`` suffix)."""
    return to_datetime(instant_ns).isoformat()
