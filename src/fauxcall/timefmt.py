"""Infer how an API formats timestamps, and render new instants the same way.

Every timestamp a fake emits must look exactly like the real API's: ``2026-10-11T01:17:26.8440216Z``
(.NET, 7 fractional digits), ``2026-10-10T21:17:26.379953-04:00`` (Python, local offset),
``2026-10-11T01:17:28.087060570Z`` (Go, nanoseconds), ``1791681447`` (Unix seconds),
``Sun, 11 Oct 2026 01:17:28 GMT`` (HTTP date) and so on.

Instants are handled as integer nanoseconds since the Unix epoch so 7- and 9-digit fractions
survive.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from email.utils import format_datetime, parsedate_to_datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from fauxcall.jsonfmt import JsonFloat

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fauxcall.types import JsonValue

NS_PER_SECOND: Final = 1_000_000_000

TimeKind = Literal["iso", "date", "time", "epoch", "http_date", "strftime"]
TzStyle = Literal["Z", "z", "+HH:MM", "+HHMM", "+HH", "none"]
FractionMode = Literal["fixed", "trim", "omit_if_zero"]
EpochUnit = Literal["s", "ms", "us", "ns"]

_NS_PER_UNIT: Final[dict[str, int]] = {"s": NS_PER_SECOND, "ms": 1_000_000, "us": 1_000, "ns": 1}

_ISO_RE: Final = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:(?P<sep>[T ])(?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"(?:[.,](?P<frac>\d{1,9}))?"
    r"(?P<tz>[Zz]|[+-]\d{2}:\d{2}|[+-]\d{4}|[+-]\d{2})?)?$"
)
_TIME_RE: Final = re.compile(
    r"^(?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?:[.,](?P<frac>\d{1,9}))?$"
)
_HTTP_DATE_RE: Final = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"\d{4} \d{2}:\d{2}:\d{2} GMT$"
)
_STRFTIME_CANDIDATES: Final = (
    "%m/%d/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%Y%m%dT%H%M%SZ",
    "%Y%m%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%d %b %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%B %d, %Y",
)

# Plausible epoch values for years 1990..2100, per unit.
_EPOCH_RANGES: Final[dict[str, tuple[float, float]]] = {
    unit: (631_152_000 * NS_PER_SECOND / ns, 4_102_444_800 * NS_PER_SECOND / ns)
    for unit, ns in _NS_PER_UNIT.items()
}


class TimeFormat(BaseModel):
    """A precise description of how one field spells instants."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: TimeKind
    separator: Literal["T", " "] = "T"
    seconds: bool = True
    fraction_digits: int = Field(default=0, ge=0, le=9)
    fraction_mode: FractionMode = "fixed"
    tz_style: TzStyle = "Z"
    epoch_unit: EpochUnit = "s"
    epoch_decimals: int = Field(default=0, ge=0, le=9)
    as_string: bool = False
    pattern: str | None = None

    # -- rendering -----------------------------------------------------------------------------

    def render(self, epoch_ns: int, tz: tzinfo = UTC) -> JsonValue:
        """Render an instant (nanoseconds since the epoch) in this format, in zone ``tz``."""
        if self.kind == "epoch":
            return self._render_epoch(epoch_ns)
        seconds, nanos = divmod(epoch_ns, NS_PER_SECOND)
        local = datetime.fromtimestamp(seconds, tz)
        match self.kind:
            case "date":
                return _date_part(local)
            case "time":
                return self._time_part(local, nanos)
            case "http_date":
                return format_datetime(local.astimezone(UTC), usegmt=True)
            case "strftime":
                return local.replace(microsecond=nanos // 1000).strftime(self.pattern or "")
            case _:
                text = _date_part(local) + self.separator + self._time_part(local, nanos)
                return text + self._offset(local)

    def _render_epoch(self, epoch_ns: int) -> JsonValue:
        per_unit = _NS_PER_UNIT[self.epoch_unit]
        if self.epoch_decimals == 0:
            whole = epoch_ns // per_unit
            return str(whole) if self.as_string else whole
        lexeme = f"{epoch_ns / per_unit:.{self.epoch_decimals}f}"
        return lexeme if self.as_string else JsonFloat(lexeme)

    def _time_part(self, local: datetime, nanos: int) -> str:
        text = f"{local.hour:02d}:{local.minute:02d}"
        if not self.seconds:
            return text
        text += f":{local.second:02d}"
        if self.fraction_digits == 0:
            return text
        digits = f"{nanos:09d}"[: self.fraction_digits]
        if self.fraction_mode == "trim":
            digits = digits.rstrip("0")
        elif self.fraction_mode == "omit_if_zero" and not digits.strip("0"):
            digits = ""
        return f"{text}.{digits}" if digits else text

    def _offset(self, local: datetime) -> str:
        if self.tz_style == "none":
            return ""
        offset = local.utcoffset() or timedelta(0)
        if self.tz_style in {"Z", "z"} and offset == timedelta(0):
            return self.tz_style
        total_minutes = int(offset.total_seconds()) // 60
        sign = "-" if total_minutes < 0 else "+"
        hours, minutes = divmod(abs(total_minutes), 60)
        if self.tz_style == "+HHMM":
            return f"{sign}{hours:02d}{minutes:02d}"
        if self.tz_style == "+HH" and minutes == 0:
            return f"{sign}{hours:02d}"
        return f"{sign}{hours:02d}:{minutes:02d}"

    # -- parsing -------------------------------------------------------------------------------

    def parse(self, value: JsonValue, default_tz: tzinfo = UTC) -> int:
        """Parse a value in this format into nanoseconds since the epoch.

        For ``kind="time"`` the result is nanoseconds since local midnight.
        Naive values are interpreted in ``default_tz``.
        """
        if self.kind == "epoch":
            return _parse_epoch(value, self.epoch_unit)
        if not isinstance(value, str):
            raise TypeError(f"expected a string timestamp, got {value!r}")
        match self.kind:
            case "time":
                return _parse_time_of_day(value)
            case "http_date":
                return _to_ns(parsedate_to_datetime(value), 0)
            case "strftime":
                parsed = datetime.strptime(value, self.pattern or "")  # noqa: DTZ007
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=default_tz)
                return _to_ns(parsed, 0)
            case _:
                return parse_iso(value, default_tz)


def _date_part(local: datetime) -> str:
    return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"


def _to_ns(moment: datetime, nanos: int) -> int:
    seconds = int(moment.replace(microsecond=0).timestamp())
    return seconds * NS_PER_SECOND + nanos


def _fraction_ns(frac: str | None) -> int:
    return int((frac or "").ljust(9, "0")[:9] or "0")


def parse_iso(value: str, default_tz: tzinfo = UTC) -> int:
    """Parse an ISO-8601 date or datetime into nanoseconds since the epoch (keeps 9 digits)."""
    match = _ISO_RE.match(value)
    if match is None:
        raise ValueError(f"not an ISO-8601 timestamp: {value!r}")
    parts = match.groupdict()
    zone = _parse_offset(parts["tz"]) if parts["tz"] else default_tz
    moment = datetime(
        int(parts["year"]),
        int(parts["month"]),
        int(parts["day"]),
        int(parts["hour"] or 0),
        int(parts["minute"] or 0),
        int(parts["second"] or 0),
        tzinfo=zone,
    )
    return _to_ns(moment, _fraction_ns(parts["frac"]))


def _parse_offset(text: str) -> tzinfo:
    if text in {"Z", "z"}:
        return UTC
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:4] or 0)
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def iso_offset(value: str) -> timedelta | None:
    """Return the UTC offset written in an ISO timestamp, or ``None`` if it has none."""
    match = _ISO_RE.match(value)
    if match is None or not match.group("tz"):
        return None
    zone = _parse_offset(match.group("tz"))
    return zone.utcoffset(None)


def _parse_time_of_day(value: str) -> int:
    match = _TIME_RE.match(value)
    if match is None:
        raise ValueError(f"not a time of day: {value!r}")
    parts = match.groupdict()
    seconds = int(parts["hour"]) * 3600 + int(parts["minute"]) * 60 + int(parts["second"] or 0)
    return seconds * NS_PER_SECOND + _fraction_ns(parts["frac"])


def _parse_epoch(value: JsonValue, unit: EpochUnit) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"not an epoch value: {value!r}")
    per_unit = _NS_PER_UNIT[unit]
    text = value.lexeme if isinstance(value, JsonFloat) else str(value)
    whole, _, frac = text.partition(".")
    try:
        ns = int(whole) * per_unit
    except ValueError as exc:
        raise ValueError(f"not an epoch value: {value!r}") from exc
    if frac:
        ns += int(frac.ljust(9, "0")[:9]) * per_unit // NS_PER_SECOND
    return ns


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def infer_time_format(samples: Sequence[JsonValue]) -> TimeFormat | None:
    """Infer a :class:`TimeFormat` that reproduces every sample exactly, or ``None``.

    Samples must all be spelled the same way (one field, many observations).
    """
    values = [s for s in samples if s is not None]
    if not values:
        return None
    candidates = _candidates(values)
    for candidate in candidates:
        if all(_round_trips(candidate, value) for value in values):
            return candidate
    return None


def _candidates(values: Sequence[JsonValue]) -> list[TimeFormat]:
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        epoch = _epoch_candidate(values, as_string=False)
        return [epoch] if epoch else []
    if all(isinstance(v, str) for v in values):
        return _string_candidates([str(v) for v in values])
    return []


def _string_candidates(texts: Sequence[str]) -> list[TimeFormat]:
    if all(_ISO_RE.match(t) for t in texts):
        return _iso_candidates(texts)
    time_matches = [m for t in texts if (m := _TIME_RE.match(t))]
    if len(time_matches) == len(texts):
        with_seconds = all(m.group("second") is not None for m in time_matches)
        return _fraction_variants(TimeFormat(kind="time", seconds=with_seconds), texts, _TIME_RE)
    if all(_HTTP_DATE_RE.match(t) for t in texts):
        return [TimeFormat(kind="http_date")]
    if all(re.fullmatch(r"-?\d+(\.\d+)?", t) for t in texts):
        epoch = _epoch_candidate(texts, as_string=True)
        return [epoch] if epoch else []
    return [TimeFormat(kind="strftime", pattern=p) for p in _STRFTIME_CANDIDATES]


def _epoch_candidate(values: Sequence[JsonValue], *, as_string: bool) -> TimeFormat | None:
    try:
        numbers = [float(str(v)) if isinstance(v, str) else float(v) for v in values]  # type: ignore[arg-type]
    except OverflowError:  # integers beyond float range are certainly not timestamps
        return None
    for unit, (low, high) in _EPOCH_RANGES.items():
        if all(low <= n <= high for n in numbers):
            decimals = max(_decimals(v) for v in values)
            return TimeFormat(
                kind="epoch",
                epoch_unit=unit,  # type: ignore[arg-type]
                epoch_decimals=decimals,
                as_string=as_string,
            )
    return None


def _decimals(value: JsonValue) -> int:
    text = value.lexeme if isinstance(value, JsonFloat) else str(value)
    return len(text.partition(".")[2]) if "." in text else 0


def _iso_candidates(texts: Sequence[str]) -> list[TimeFormat]:
    matches = [m for t in texts if (m := _ISO_RE.match(t))]
    if all(m.group("hour") is None for m in matches):
        return [TimeFormat(kind="date")]
    if any(m.group("hour") is None for m in matches):
        return []
    separators = {m.group("sep") for m in matches}
    if len(separators) != 1:
        return []
    tz_styles = {_tz_style(m.group("tz")) for m in matches}
    tz_style = _merge_tz_styles(tz_styles)
    if tz_style is None:
        return []
    base = TimeFormat(
        kind="iso",
        separator=separators.pop(),  # type: ignore[arg-type]
        seconds=all(m.group("second") is not None for m in matches),
        tz_style=tz_style,
    )
    return _fraction_variants(base, texts, _ISO_RE)


def _fraction_variants(
    base: TimeFormat, texts: Sequence[str], regex: re.Pattern[str]
) -> list[TimeFormat]:
    lengths = sorted({len(m.group("frac") or "") for t in texts if (m := regex.match(t))})
    longest = lengths[-1]
    if longest == 0:
        return [base]
    variants: list[TimeFormat] = []
    if len(lengths) == 1:
        variants.append(base.model_copy(update={"fraction_digits": longest}))
    if lengths == [0, longest]:
        variants.append(
            base.model_copy(update={"fraction_digits": longest, "fraction_mode": "omit_if_zero"})
        )
    variants.append(base.model_copy(update={"fraction_digits": longest, "fraction_mode": "trim"}))
    return variants


def _tz_style(text: str | None) -> TzStyle:
    if not text:
        return "none"
    if text in {"Z", "z"}:
        return text  # type: ignore[return-value]
    if ":" in text:
        return "+HH:MM"
    return "+HHMM" if len(text) == 5 else "+HH"  # noqa: PLR2004 - "+HHMM"


def _merge_tz_styles(styles: set[TzStyle]) -> TzStyle | None:
    if len(styles) == 1:
        return next(iter(styles))
    # "Z" for UTC values and "+HH:MM" for others is one consistent style (.NET round-trip "o").
    if styles == {"Z", "+HH:MM"}:
        return "Z"
    return None


def _round_trips(fmt: TimeFormat, value: JsonValue) -> bool:
    try:
        if fmt.kind == "time":
            ns = fmt.parse(value)
            midnight = datetime(2000, 1, 1, tzinfo=UTC)
            rendered = fmt.render(_to_ns(midnight, 0) + ns)
        else:
            zone: tzinfo = UTC
            if fmt.kind == "iso" and isinstance(value, str):
                offset = iso_offset(value)
                zone = timezone(offset) if offset is not None else UTC
            rendered = fmt.render(fmt.parse(value, zone), zone)
    except (ValueError, TypeError, OverflowError):
        return False
    if isinstance(rendered, JsonFloat) and isinstance(value, JsonFloat):
        return rendered.lexeme == value.lexeme
    return rendered == value


def looks_like_time(value: JsonValue) -> bool:
    """Cheap check: could this single value be a timestamp?"""
    return infer_time_format([value]) is not None
