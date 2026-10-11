"""Response bodies of timeapi.io, computed from the tz database and the fake's clock.

Two API generations live side by side on the real server and share the same logic:

* ``/api/v1/...`` — snake_case bodies, ISO times with 6 fraction digits and an offset.
* ``/api/...`` (v0.99, "legacy") — camelCase bodies, .NET ``DateTime`` spellings (7 trimmed
  fraction digits, no offset), NodaTime ``Offset``/``Duration`` objects, US date strings.

Everything here is pure (zone + instant in, JSON out) so it is easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from fauxcall_timeapi import dotnet, tzengine
from fauxcall_timeapi.dotnet import (
    NS_PER_TICK,
    TICKS_PER_SECOND,
    LocalDateTime,
    TimeSpan,
    iso_local,
    json_double,
    offset_text,
    short_time,
    us_date,
)

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue

NS_PER_SECOND: Final = 1_000_000_000
Ambiguity = Literal["", "earlier", "later"]


@dataclass(frozen=True, slots=True)
class ZoneState:
    """A zone at one instant: the current interval and the DST interval it reports."""

    zone: str
    instant_ticks: int
    current: tzengine.Interval
    dst: tzengine.Interval | None
    """The current interval if it has savings, else the next one if it does, else ``None``."""

    @property
    def local(self) -> LocalDateTime:
        """Wall-clock time in the zone."""
        return local_at(self.instant_ticks, self.current.utc_offset)

    @property
    def utc(self) -> LocalDateTime:
        """Wall-clock time in UTC."""
        return local_at(self.instant_ticks, 0)

    @property
    def dst_active(self) -> bool:
        """Whether daylight saving (positive or negative) is in effect."""
        return self.current.savings != 0


def local_at(instant_ticks: int, offset_seconds: int) -> LocalDateTime:
    """The wall-clock time ``offset_seconds`` east of UTC at ``instant_ticks`` (Unix ticks)."""
    seconds, ticks = divmod(instant_ticks + offset_seconds * TICKS_PER_SECOND, TICKS_PER_SECOND)
    return LocalDateTime.from_epoch(seconds, ticks)


def state(zone: str, instant_ticks: int) -> ZoneState:
    """The zone's state at an instant."""
    current, following = tzengine.interval_at(zone, instant_ticks / TICKS_PER_SECOND)
    if current.savings:
        dst: tzengine.Interval | None = current
    elif following is not None and following.savings:
        dst = following
    else:
        dst = None
    return ZoneState(zone, instant_ticks, current, dst)


def now_ticks(now_ns: int) -> int:
    """The clock in Unix ticks (100 ns), as .NET reads it."""
    return now_ns // NS_PER_TICK


# -- v1 ----------------------------------------------------------------------------------------


def _v1_time(value: LocalDateTime, offset: int) -> str:
    return iso_local(value, digits=6, trim=False) + offset_text(offset)


def _v1_instant(seconds: int) -> str | None:
    if seconds in (tzengine.MIN_INSTANT, tzengine.MAX_INSTANT):
        return None
    return _v1_time(local_at(seconds * TICKS_PER_SECOND, 0), 0)


def v1_current_time(s: ZoneState) -> JsonObject:
    """``CurrentTime`` of ``/api/v1/time/current/*``."""
    local = s.local
    stamp = iso_local(local, digits=6, trim=False)
    return {
        "date_time": stamp + offset_text(s.current.utc_offset),
        "date": stamp[:10],
        "time": stamp[11:],
        "day_of_week": local.day_name,
        "dst_active": s.dst_active,
        "timezone": s.zone,
        "utc_offset_seconds": s.current.utc_offset,
    }


def v1_timezone(s: ZoneState) -> JsonObject:
    """``TimezoneModel`` of ``/api/v1/timezone/*``."""
    dst = s.dst
    return {
        "timezone": s.zone,
        "current_utc_offset_seconds": s.current.utc_offset,
        "standard_utc_offset_seconds": s.current.standard_offset,
        "dst_utc_offset_seconds": dst.utc_offset if dst else 0,
        "has_dst": dst is not None,
        "dst_offset_seconds": dst.savings if dst else 0,
        "dst_active": s.dst_active,
        "dst_from": _v1_instant(dst.start) if dst else None,
        "dst_until": _v1_instant(dst.end) if dst else None,
        "local_time": _v1_time(s.local, s.current.utc_offset),
        "day_of_week": s.local.day_name,
        "utc_time": _v1_time(s.utc, 0),
        "unix_timestamp": s.instant_ticks // TICKS_PER_SECOND,
    }


def v1_utc(instant_ticks: int) -> JsonObject:
    """``/api/v1/time/current/utc``: up to 7 fraction digits (trailing zeros trimmed) and ``Z``."""
    return {"utc_time": iso_local(local_at(instant_ticks, 0)) + "Z"}


def v1_unix(ticks: int, unit: Literal["s", "ms", "us", "ns"]) -> JsonObject:
    """``/api/v1/time/current/unix*`` (nanoseconds in steps of 100: .NET tick resolution)."""
    value = {
        "s": ticks // TICKS_PER_SECOND,
        "ms": ticks // 10_000,
        "us": ticks // 10,
        "ns": ticks * NS_PER_TICK,
    }[unit]
    return {"unix_timestamp": value}


# -- legacy (v0.99) --------------------------------------------------------------------------------


def _offset(seconds: int) -> JsonObject:
    """A NodaTime ``Offset``."""
    return {
        "seconds": seconds,
        "milliseconds": seconds * 1000,
        "ticks": seconds * TICKS_PER_SECOND,
        "nanoseconds": seconds * NS_PER_SECOND,
    }


def _duration(seconds: int) -> JsonObject:
    """A NodaTime ``Duration`` of whole seconds."""
    days, rest = divmod(seconds, 86_400)
    hours, rest_h = divmod(rest, 3600)
    minutes, secs = divmod(rest_h, 60)
    return {
        "days": days,
        "nanosecondOfDay": rest * NS_PER_SECOND,
        "hours": hours,
        "minutes": minutes,
        "seconds": secs,
        "milliseconds": 0,
        "subsecondTicks": 0,
        "subsecondNanoseconds": 0,
        "bclCompatibleTicks": seconds * TICKS_PER_SECOND,
        "totalDays": json_double(seconds / 86_400),
        "totalHours": json_double(seconds / 3600),
        "totalMinutes": json_double(seconds / 60),
        "totalSeconds": seconds,
        "totalMilliseconds": seconds * 1000,
        "totalTicks": seconds * TICKS_PER_SECOND,
        "totalNanoseconds": seconds * NS_PER_SECOND,
    }


def _legacy_instant(seconds: int) -> str | None:
    if seconds in (tzengine.MIN_INSTANT, tzengine.MAX_INSTANT):
        return None
    return iso_local(local_at(seconds * TICKS_PER_SECOND, 0)) + "Z"


def _result_fields(local: LocalDateTime) -> JsonObject:
    return {
        "year": local.year,
        "month": local.month,
        "day": local.day,
        "hour": local.hour,
        "minute": local.minute,
        "seconds": local.second,
        "milliSeconds": local.ticks // 10_000,
        "dateTime": iso_local(local),
        "date": us_date(local),
        "time": short_time(local),
    }


def legacy_current_time(s: ZoneState) -> JsonObject:
    """``CurrentTime`` of ``/api/time/current/*``."""
    return {
        **_result_fields(s.local),
        "timeZone": s.zone,
        "dayOfWeek": s.local.day_name,
        "dstActive": s.dst_active,
    }


def legacy_timezone(s: ZoneState) -> JsonObject:
    """``TimeZoneData`` of ``/api/timezone/*``."""
    dst = s.dst
    interval: JsonValue = None
    if dst is not None:
        bounded = dst.bounded_start and dst.bounded_end
        interval = {
            "dstName": dst.name,
            "dstOffsetToUtc": _offset(dst.utc_offset),
            "dstOffsetToStandardTime": _offset(dst.savings),
            "dstStart": _legacy_instant(dst.start),
            "dstEnd": _legacy_instant(dst.end),
            "dstDuration": _duration(dst.end - dst.start) if bounded else None,
        }
    return {
        "timeZone": s.zone,
        "currentLocalTime": iso_local(s.local),
        "currentUtcOffset": _offset(s.current.utc_offset),
        "standardUtcOffset": _offset(s.current.standard_offset),
        "hasDayLightSaving": dst is not None,
        "isDayLightSavingActive": s.dst_active,
        "dstInterval": interval,
    }


# -- conversion and calculation ------------------------------------------------------------------


class OutOfRangeError(ValueError):
    """A result outside .NET ``DateTime`` (years 1-9999): the real server answers a 500 page."""


def _in_range(local: LocalDateTime) -> LocalDateTime:
    if not dotnet.MIN_YEAR <= local.year <= dotnet.MAX_YEAR:
        raise OutOfRangeError(local.year)
    return local


class AmbiguousTimeError(ValueError):
    """The local time occurs twice and no ``dstAmbiguity`` was given."""


def resolve(zone: str, local: LocalDateTime, ambiguity: Ambiguity) -> tuple[int, LocalDateTime]:
    """A local time in ``zone`` -> (instant ticks, the local time actually used).

    Ambiguous times need ``earlier``/``later``; skipped times (DST gaps) are shifted forward by
    the length of the gap, like NodaTime's lenient resolver.
    """
    seconds = local.epoch_seconds
    candidates = tzengine.local_candidates(zone, seconds)
    if len(candidates) > 1:
        if not ambiguity:
            raise AmbiguousTimeError(zone)
        chosen = candidates[0] if ambiguity == "earlier" else candidates[-1]
        offset = chosen.utc_offset
    elif candidates:
        offset = candidates[0].utc_offset
    else:
        offset = tzengine.gap_offset(zone, seconds)
    instant = (seconds - offset) * TICKS_PER_SECOND + local.ticks
    return instant, local_at(instant, tzengine.offset_at(zone, instant / TICKS_PER_SECOND))


def conversion(
    from_zone: str, local: LocalDateTime, to_zone: str, ambiguity: Ambiguity
) -> JsonObject:
    """``/api/conversion/converttimezone``."""
    instant, used = resolve(from_zone, local, ambiguity)
    target = state(to_zone, instant)
    _in_range(target.local)
    return {
        "fromTimezone": from_zone,
        "fromDateTime": iso_local(used),
        "toTimeZone": to_zone,
        "conversionResult": {
            **_result_fields(target.local),
            "timeZone": to_zone,
            "dstActive": target.dst_active,
        },
    }


def calculation(zone: str, instant: int, span: TimeSpan, sign: Literal[1, -1]) -> JsonObject:
    """``/api/calculation/*``: instant ± span, shown in ``zone``."""
    original = state(zone, instant)
    result = state(zone, instant + sign * span.total_ticks)
    _in_range(result.local)
    return {
        "timeZone": zone,
        "originalDateTime": iso_local(original.local),
        "usedTimeSpan": str(span),
        "calculationResult": {**_result_fields(result.local), "dstActive": result.dst_active},
    }
