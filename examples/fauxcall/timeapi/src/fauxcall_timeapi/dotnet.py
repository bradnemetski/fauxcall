"""How the real server (ASP.NET Core) parses inputs and spells outputs.

Every rule here comes from recordings: number binding (invariant culture, thousands separators,
``float`` precision), the accepted date/time-span formats, IP address parsing, and the spellings of
``DateTime``, NodaTime ``Instant`` and ``double`` values in JSON.
"""

from __future__ import annotations

import ipaddress
import math
import re
import struct
from dataclasses import dataclass
from typing import Final

TICKS_PER_SECOND: Final = 10_000_000
NS_PER_TICK: Final = 100
SECONDS_PER_DAY: Final = 86_400
DAY_NAMES: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# -- numbers -----------------------------------------------------------------------------------

_FLOAT_RE: Final = re.compile(
    r"^\s*[+-]?(?:(?:[0-9][0-9,]*)(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*$", re.ASCII
)
_SPECIAL_FLOATS: Final = {
    "nan": math.nan,
    "infinity": math.inf,
    "+infinity": math.inf,
    "-infinity": -math.inf,
    "∞": math.inf,
    "-∞": -math.inf,
}


def parse_float(text: str) -> float | None:
    """``float.TryParse(text, NumberStyles.Float | AllowThousands, InvariantCulture)``.

    Group separators are accepted (and ignored) in the integer part: ``52,3667`` is 523667. The
    value is rounded to single precision, as the server binds ``float`` parameters.
    """
    special = _SPECIAL_FLOATS.get(text.strip().lower())
    if special is not None:
        return special
    if not _FLOAT_RE.match(text):
        return None
    value = float(text.strip().replace(",", ""))
    return to_single(value)


def to_single(value: float) -> float:
    """``value`` rounded to IEEE single precision (``inf`` when out of range)."""
    try:
        return float(struct.unpack(">f", struct.pack(">f", value))[0])
    except OverflowError:
        return math.copysign(math.inf, value)


def json_double(value: float) -> int | float:
    """A ``double`` as System.Text.Json writes it: integral values without a fraction."""
    if math.isfinite(value) and value == int(value):
        return int(value)
    return value


# -- date and time ---------------------------------------------------------------------------------

_LOCAL_RE: Final = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{6}))?$", re.ASCII
)
_DATE_RE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$", re.ASCII)
_SPAN_RE: Final = re.compile(r"^(\d{1,8}):(\d{2}):(\d{2}):(\d{2})(?:\.(\d{3}))?$", re.ASCII)
MAX_SPAN_TICKS: Final = 2**63 - 1
"""``TimeSpan.MaxValue`` (10675199.02:48:05.4775807): longer spans do not parse (cc-maxspan)."""
MIN_YEAR: Final = 1
MAX_YEAR: Final = 9999
_IPV4_PART_RE: Final = re.compile(r"^(?:0[xX][0-9a-fA-F]*|0[0-7]*|[1-9][0-9]*)$", re.ASCII)


@dataclass(frozen=True, slots=True)
class LocalDateTime:
    """A wall-clock date and time, with tick (100 ns) precision."""

    year: int
    month: int
    day: int
    hour: int = 0
    minute: int = 0
    second: int = 0
    ticks: int = 0
    """Sub-second ticks, 0 … 9 999 999."""

    @property
    def epoch_seconds(self) -> int:
        """Seconds since the local epoch 1970-01-01T00:00:00 (wall clock, no zone)."""
        return (
            days_from_civil(self.year, self.month, self.day) * SECONDS_PER_DAY
            + self.hour * 3600
            + self.minute * 60
            + self.second
        )

    @classmethod
    def from_epoch(cls, seconds: int, ticks: int = 0) -> LocalDateTime:
        """The wall-clock time ``seconds`` after the local epoch (plus sub-second ``ticks``)."""
        days, rest = divmod(seconds, SECONDS_PER_DAY)
        year, month, day = civil_from_days(days)
        hour, rest = divmod(rest, 3600)
        minute, second = divmod(rest, 60)
        return cls(year, month, day, hour, minute, second, ticks)

    @property
    def weekday(self) -> int:
        """ISO weekday index, Monday = 0."""
        return (days_from_civil(self.year, self.month, self.day) + 3) % 7

    @property
    def day_name(self) -> str:
        """English day name."""
        return DAY_NAMES[self.weekday]

    @property
    def day_of_year(self) -> int:
        """1-based day of the year."""
        return (
            days_from_civil(self.year, self.month, self.day) - days_from_civil(self.year, 1, 1) + 1
        )


def days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 of a proleptic Gregorian date (any year)."""
    y = year - (month <= 2)  # noqa: PLR2004
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1  # noqa: PLR2004
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def civil_from_days(days: int) -> tuple[int, int, int]:
    """Inverse of :func:`days_from_civil`."""
    z = days + 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + (3 if mp < 10 else -9)  # noqa: PLR2004
    return y + (month <= 2), month, day  # noqa: PLR2004


def _valid_date(year: int, month: int, day: int) -> bool:
    if not (1 <= year <= 9999 and 1 <= month <= 12):  # noqa: PLR2004
        return False
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
    return 1 <= day <= days


def parse_local(text: str) -> LocalDateTime | None:
    """``yyyy-MM-dd HH:mm:ss`` or ``yyyy-MM-dd HH:mm:ss.ffffff`` (exactly), else ``None``."""
    match = _LOCAL_RE.match(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match[i]) for i in range(1, 7))
    if not _valid_date(year, month, day) or hour > 23 or minute > 59 or second > 59:  # noqa: PLR2004
        return None
    ticks = int(match[7]) * 10 if match[7] else 0
    return LocalDateTime(year, month, day, hour, minute, second, ticks)


def parse_date(text: str) -> LocalDateTime | None:
    """``yyyy-MM-dd`` (exactly), else ``None``."""
    match = _DATE_RE.match(text)
    if match is None:
        return None
    year, month, day = (int(match[i]) for i in range(1, 4))
    return LocalDateTime(year, month, day) if _valid_date(year, month, day) else None


@dataclass(frozen=True, slots=True)
class TimeSpan:
    """``d:hh:mm:ss[.fff]``."""

    days: int
    hours: int
    minutes: int
    seconds: int
    milliseconds: int

    @property
    def total_ticks(self) -> int:
        """The span in ticks."""
        seconds = ((self.days * 24 + self.hours) * 60 + self.minutes) * 60 + self.seconds
        return seconds * TICKS_PER_SECOND + self.milliseconds * 10_000

    def __str__(self) -> str:
        text = f"{self.days}:{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}"
        if self.milliseconds:
            text += "." + f"{self.milliseconds:03d}".rstrip("0")
        return text


def parse_span(text: str) -> TimeSpan | None:
    r"""``TimeSpan.TryParseExact`` with ``d\:hh\:mm\:ss`` / ``d\:hh\:mm\:ss\.fff``."""
    match = _SPAN_RE.match(text)
    if match is None:
        return None
    days, hours, minutes, seconds = (int(match[i]) for i in range(1, 5))
    if hours > 23 or minutes > 59 or seconds > 59:  # noqa: PLR2004
        return None
    span = TimeSpan(days, hours, minutes, seconds, int(match[5]) if match[5] else 0)
    return span if span.total_ticks <= MAX_SPAN_TICKS else None


def fraction(ticks: int, digits: int, *, trim: bool) -> str:
    """``.fffffff``-style fraction of ``ticks`` (``trim``: .NET ``F`` specifiers, may be empty)."""
    text = f"{ticks:07d}"[:digits]
    if trim:
        text = text.rstrip("0")
    return f".{text}" if text else ""


def offset_text(seconds: int) -> str:
    """``+HH:MM``."""
    sign = "-" if seconds < 0 else "+"
    hours, rest = divmod(abs(seconds), 3600)
    return f"{sign}{hours:02d}:{rest // 60:02d}"


def iso_local(value: LocalDateTime, *, digits: int = 7, trim: bool = True) -> str:
    """``yyyy-MM-ddTHH:mm:ss`` + fraction (a ``DateTime`` of kind Unspecified in JSON)."""
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}T"
        f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
        + fraction(value.ticks, digits, trim=trim)
    )


def us_date(value: LocalDateTime) -> str:
    """``MM/dd/yyyy``."""
    return f"{value.month:02d}/{value.day:02d}/{value.year:04d}"


def short_time(value: LocalDateTime) -> str:
    """``HH:mm``."""
    return f"{value.hour:02d}:{value.minute:02d}"


# -- IP addresses ----------------------------------------------------------------------------------


def parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``IPAddress.TryParse``: IPv6 (also ``[...]``), or IPv4 in any ``inet_aton`` form.

    ``8.8.8`` is 8.8.0.8, parts may be octal (``010``) or hex (``0x8``), ``134744072`` is one
    32-bit number. Whitespace, junk and overflow (``4294967296``) are rejected (ip-junk, ip-big).
    """
    if not text or not text.isascii() or text != text.strip():
        return None
    if ":" in text:
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        try:
            return ipaddress.IPv6Address(text.split("%", 1)[0])
        except ValueError:
            return None
    parts = text.split(".")
    if len(parts) > 4 or not all(_IPV4_PART_RE.match(part) for part in parts):  # noqa: PLR2004
        return None
    values = [_inet_number(part) for part in parts]
    head, last = values[:-1], values[-1]
    if any(v > 255 for v in head) or last >= 256 ** (5 - len(values)):  # noqa: PLR2004
        return None
    number = last
    for i, value in enumerate(head):
        number += value << (8 * (3 - i))
    return ipaddress.IPv4Address(number)


def _inet_number(part: str) -> int:
    if part[:2].lower() == "0x":
        return int(part[2:] or "0", 16)
    if len(part) > 1 and part.startswith("0"):
        return int(part, 8)
    return int(part)


_JALALI_BREAKS: Final = (
    -61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
    1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178,
)  # fmt: skip


def _jal_cal(jy: int) -> tuple[int, int]:
    """(leap remainder, Gregorian March day of 1 Farvardin) — jalaali-js ``jalCal``."""
    gy = jy + 621
    leap_j = -14
    jp = _JALALI_BREAKS[0]
    jump = 0
    for jm in _JALALI_BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += jump // 33 * 8 + (jump % 33) // 4
        jp = jm
    n = jy - jp
    leap_j += n // 33 * 8 + ((n % 33) + 3) // 4
    if jump % 33 == 4 and jump - n == 4:  # noqa: PLR2004
        leap_j += 1
    leap_g = gy // 4 - (gy // 100 + 1) * 3 // 4 - 150
    march = 20 + leap_j - leap_g
    if jump - n < 6:  # noqa: PLR2004
        n = n - jump + (jump + 4) // 33 * 33
    leap = ((n + 1) % 33 - 1) % 4
    if leap == -1:
        leap = 4
    return leap, march


def persian_date(year: int, month: int, day: int) -> tuple[int, int, int]:
    """A Gregorian date in the Solar Hijri calendar (.NET ``PersianCalendar``, jalaali-js)."""
    days = days_from_civil(year, month, day)
    jy = year - 621
    _, march = _jal_cal(jy)
    k = days - days_from_civil(year, 3, march)
    if k < 0:
        jy -= 1
        _, march = _jal_cal(jy)
        k = days - days_from_civil(year - 1, 3, march)
    if k < 186:  # noqa: PLR2004 - six 31-day months
        return jy, k // 31 + 1, k % 31 + 1
    k -= 186
    return jy, k // 30 + 7, k % 30 + 1
