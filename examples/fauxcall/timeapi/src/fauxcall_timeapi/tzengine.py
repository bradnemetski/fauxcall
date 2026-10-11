"""Time zone intervals the way timeapi.io (a .NET service on NodaTime) sees them.

Python's ``zoneinfo`` answers "what is the offset now", but timeapi.io also reports the *standard*
offset, the *savings* of the current or next daylight-saving interval and that interval's bounds,
with NodaTime's semantics: savings come straight from the tz database rules, so they can be negative
(``Europe/Dublin`` in winter, ``Africa/Casablanca`` during Ramadan). This module reads the compiled
tz database (the ``tzdata`` package) directly: the TZif transitions carry an ``isdst`` flag per
period, and the POSIX rule in the TZif footer continues them into the future.

An interval's standard offset is the offset of the nearest non-DST period (the previous one, else
the next); its savings are ``utc offset - standard offset``.
"""

from __future__ import annotations

import calendar
import importlib.resources
import re
import struct
from dataclasses import dataclass
from functools import cache
from typing import Final

SECONDS_PER_DAY: Final = 86_400
MIN_INSTANT: Final = -(2**63)
MAX_INSTANT: Final = 2**63 - 1
_FOOTER_YEARS: Final = 3
"""Years of POSIX-rule transitions generated past the last explicit one (and past "now")."""


@dataclass(frozen=True, slots=True)
class Interval:
    """One period of constant offset: ``[start, end)`` in Unix seconds."""

    start: int
    end: int
    utc_offset: int
    is_dst: bool
    name: str = ""
    standard_offset: int = 0

    @property
    def savings(self) -> int:
        """Daylight savings in seconds (negative for "winter time" DST)."""
        return self.utc_offset - self.standard_offset

    @property
    def bounded_start(self) -> bool:
        """Whether the interval has a real start (not the beginning of time)."""
        return self.start != MIN_INSTANT

    @property
    def bounded_end(self) -> bool:
        """Whether the interval has a real end (not the end of time)."""
        return self.end != MAX_INSTANT


@dataclass(frozen=True, slots=True)
class _Period:
    utc_offset: int
    is_dst: bool
    name: str = ""


# -- TZif ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TzifData:
    transitions: list[int]
    periods: list[_Period]
    initial: _Period
    footer: str


def _read_tzif(data: bytes) -> _TzifData:
    """Parse a TZif (v2+) file: transitions, period types and the POSIX footer."""
    if data[:4] != b"TZif":
        raise ValueError("not a TZif file")
    version = data[4:5]
    header = struct.Struct(">4s1s15x6l")

    def counts(offset: int) -> tuple[int, int, int, int, int, int]:
        fields = header.unpack_from(data, offset)
        return fields[2], fields[3], fields[4], fields[5], fields[6], fields[7]

    isutc, isstd, leap, timecnt, typecnt, charcnt = counts(0)
    offset = header.size
    time_size = 4
    if version >= b"2":
        offset += (
            timecnt * 4 + timecnt + typecnt * 6 + charcnt + leap * 8 + isstd + isutc
        )  # skip the v1 block
        isutc, isstd, leap, timecnt, typecnt, charcnt = counts(offset)
        offset += header.size
        time_size = 8
    fmt = ">q" if time_size == 8 else ">l"  # noqa: PLR2004
    transitions = [struct.unpack_from(fmt, data, offset + i * time_size)[0] for i in range(timecnt)]
    offset += timecnt * time_size
    indexes = list(data[offset : offset + timecnt])
    offset += timecnt
    raw_types = [struct.unpack_from(">lBB", data, offset + i * 6) for i in range(typecnt)]
    offset += typecnt * 6
    chars = data[offset : offset + charcnt]
    types = [
        _Period(utoff, bool(isdst), chars[abbr:].split(b"\0", 1)[0].decode("ascii"))
        for utoff, isdst, abbr in raw_types
    ]
    offset += charcnt + leap * (time_size + 4) + isstd + isutc
    footer = ""
    if version >= b"2":
        rest = data[offset:].split(b"\n")
        footer = rest[1].decode("ascii") if len(rest) > 1 else ""
    periods = [types[i] for i in indexes]
    initial = types[0] if types else _Period(0, is_dst=False)
    return _TzifData(transitions, periods, initial, footer)


# -- POSIX TZ rules --------------------------------------------------------------------------------

_NAME = r"(?:<[^>]+>|[A-Za-z]{3,})"
_OFFSET = r"[+-]?\d{1,3}(?::\d{1,2}(?::\d{1,2})?)?"
_FOOTER_RE: Final = re.compile(
    rf"^(?P<std>{_NAME})(?P<stdoff>{_OFFSET})"
    rf"(?:(?P<dst>{_NAME})(?P<dstoff>{_OFFSET})?(?:,(?P<start>[^,]+),(?P<end>[^,]+))?)?$"
)


def _hms(text: str) -> int:
    sign = -1 if text.startswith("-") else 1
    parts = [int(p) for p in text.lstrip("+-").split(":")]
    parts += [0] * (3 - len(parts))
    return sign * (parts[0] * 3600 + parts[1] * 60 + parts[2])


@dataclass(frozen=True, slots=True)
class _Rule:
    std_offset: int
    dst_offset: int
    start: str
    end: str
    std_name: str
    dst_name: str


def _parse_footer(footer: str) -> _Rule | int | None:
    """The footer's rule; a bare int when it is a fixed offset; ``None`` when absent."""
    if not footer:
        return None
    match = _FOOTER_RE.match(footer)
    if match is None:
        return None
    std_offset = -_hms(match["stdoff"])  # POSIX offsets are west-positive
    if match["dst"] is None or match["start"] is None:
        return std_offset
    dst_offset = -_hms(match["dstoff"]) if match["dstoff"] else std_offset + 3600
    return _Rule(
        std_offset,
        dst_offset,
        match["start"],
        match["end"],
        match["std"].strip("<>"),
        match["dst"].strip("<>"),
    )


def _rule_day(spec: str, year: int) -> tuple[int, int]:
    """(days since the epoch of the local date, seconds after local midnight) for ``spec``."""
    date_part, _, time_part = spec.partition("/")
    seconds = _hms(time_part) if time_part else 7200
    if date_part.startswith("M"):
        month, week, weekday = (int(x) for x in date_part[1:].split("."))
        first_weekday, days_in_month = calendar.monthrange(year, month)  # Monday=0
        first_sunday_based = (first_weekday + 1) % 7  # Sunday=0
        day = 1 + (weekday - first_sunday_based) % 7 + (week - 1) * 7
        if week == 5:  # noqa: PLR2004 - "last" week
            while day > days_in_month:
                day -= 7
        days = _days_from_civil(year, month, day)
    elif date_part.startswith("J"):
        n = int(date_part[1:])  # 1..365, Feb 29 never counted
        days = _days_from_civil(year, 1, 1) + n - 1
        if calendar.isleap(year) and n >= 60:  # noqa: PLR2004
            days += 1
    else:
        days = _days_from_civil(year, 1, 1) + int(date_part)
    return days, seconds


def _days_from_civil(year: int, month: int, day: int) -> int:
    return calendar.timegm((year, month, day, 0, 0, 0)) // SECONDS_PER_DAY


def _rule_transitions(rule: _Rule, year: int) -> list[tuple[int, _Period]]:
    """The two transitions of ``year``: (instant, period starting there), sorted."""
    start_days, start_secs = _rule_day(rule.start, year)
    end_days, end_secs = _rule_day(rule.end, year)
    dst_start = start_days * SECONDS_PER_DAY + start_secs - rule.std_offset
    dst_end = end_days * SECONDS_PER_DAY + end_secs - rule.dst_offset
    pairs = [
        (dst_start, _Period(rule.dst_offset, is_dst=True, name=rule.dst_name)),
        (dst_end, _Period(rule.std_offset, is_dst=False, name=rule.std_name)),
    ]
    return sorted(pairs, key=lambda pair: pair[0])


# -- zones -----------------------------------------------------------------------------------------


@cache
def _zone_files() -> dict[str, str]:
    """Lower-cased zone id -> canonical id, for every zone in the bundled tz database."""
    root = importlib.resources.files("tzdata").joinpath("zones")
    names = [line.strip() for line in root.read_text(encoding="utf-8").splitlines()]
    return {name.lower(): name for name in names if name and name != "Factory"}


def canonical_zone(name: str) -> str | None:
    """The zone id as the tz database spells it, matched case-insensitively (``None``: unknown)."""
    return _zone_files().get(name.lower())


def zone_ids() -> list[str]:
    """Every zone id in the bundled tz database."""
    return sorted(_zone_files().values())


@cache
def _tzif(zone: str) -> _TzifData:
    package, _, leaf = f"tzdata.zoneinfo.{zone.replace('/', '.')}".rpartition(".")
    data = importlib.resources.files(package).joinpath(leaf).read_bytes()
    return _read_tzif(data)


@cache
def _raw_periods(zone: str, until_year: int) -> list[tuple[int, _Period]]:
    """(start instant, period) from the beginning of time to the end of ``until_year``."""
    data = _tzif(zone)
    items: list[tuple[int, _Period]] = [(MIN_INSTANT, data.initial)]
    items += list(zip(data.transitions, data.periods, strict=True))
    footer = _parse_footer(data.footer)
    if isinstance(footer, _Rule):
        last = items[-1][0]
        first_year = 1970 if last == MIN_INSTANT else _year_of(last)
        for year in range(first_year, until_year + 1):
            items += [(at, period) for at, period in _rule_transitions(footer, year) if at > last]
    merged: list[tuple[int, _Period]] = []
    for start, period in items:
        if merged and merged[-1][1] == period:
            continue
        merged.append((start, period))
    return merged


def _year_of(instant: int) -> int:
    return 1970 + int(instant // (365.2425 * SECONDS_PER_DAY))


@cache
def intervals(zone: str, until_year: int) -> list[Interval]:
    """All intervals of ``zone`` up to ``until_year`` (+ the open-ended one after)."""
    raw = _raw_periods(zone, until_year)
    result: list[Interval] = []
    for i, (start, period) in enumerate(raw):
        end = raw[i + 1][0] if i + 1 < len(raw) else MAX_INSTANT
        result.append(Interval(start, end, period.utc_offset, period.is_dst, period.name))
    standard: list[Interval] = []
    for i, interval in enumerate(result):
        if not interval.is_dst:
            std = interval.utc_offset
        else:
            before = next((r for r in reversed(result[:i]) if not r.is_dst), None)
            after = next((r for r in result[i + 1 :] if not r.is_dst), None)
            neighbour = before or after
            std = neighbour.utc_offset if neighbour else interval.utc_offset
        standard.append(
            Interval(
                interval.start,
                interval.end,
                interval.utc_offset,
                interval.is_dst,
                interval.name,
                std,
            )
        )
    return standard


def interval_at(zone: str, instant: float) -> tuple[Interval, Interval | None]:
    """The interval containing ``instant`` (Unix seconds) and the one after it, if any."""
    year = _year_of(int(instant)) + _FOOTER_YEARS
    items = intervals(zone, year)
    lo, hi = 0, len(items) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if items[mid].start <= instant:
            lo = mid
        else:
            hi = mid - 1
    current = items[lo]
    following = items[lo + 1] if lo + 1 < len(items) else None
    return current, following


def offset_at(zone: str, instant: float) -> int:
    """UTC offset (seconds) of ``zone`` at ``instant``."""
    return interval_at(zone, instant)[0].utc_offset


def local_candidates(zone: str, local_seconds: int) -> list[Interval]:
    """Intervals in which the local wall time ``local_seconds`` (since the local epoch) occurs.

    Empty for a skipped time (DST gap); two for an ambiguous one (earlier first).
    """
    window = 18 * 3600
    found: list[Interval] = []
    for guess in range(local_seconds - window, local_seconds + window + 1, window // 3):
        interval, _ = interval_at(zone, guess)
        instant = local_seconds - interval.utc_offset
        if interval.start <= instant < interval.end and interval not in found:
            found.append(interval)
    found.sort(key=lambda interval: interval.start)
    return found


def gap_offset(zone: str, local_seconds: int) -> int:
    """For a skipped local time: the offset in effect just before the gap.

    ``local - offset`` is then the instant the wall clock jumped, shifted forward by the time
    elapsed since the start of the gap (NodaTime's lenient "return forward shifted").
    """
    window = 18 * 3600
    for guess in range(local_seconds - window, local_seconds + window + 1, window // 3):
        interval, following = interval_at(zone, guess)
        if following is None or not interval.bounded_end:
            continue
        before = interval.end + interval.utc_offset
        after = interval.end + following.utc_offset
        if before <= local_seconds < after:
            return interval.utc_offset
    return interval_at(zone, local_seconds)[0].utc_offset
