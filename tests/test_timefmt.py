from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from fauxcall.jsonfmt import JsonFloat
from fauxcall.timefmt import (
    NS_PER_SECOND,
    TimeFormat,
    infer_time_format,
    iso_offset,
    looks_like_time,
    parse_iso,
)

if TYPE_CHECKING:
    from fauxcall.types import JsonValue


def _text(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


# 2026-10-11T01:17:26.844021600Z
INSTANT = (
    int(datetime(2026, 10, 11, 1, 17, 26, tzinfo=UTC).timestamp()) * NS_PER_SECOND + 844_021_600
)


@pytest.mark.parametrize(
    "samples",
    [
        ["2026-10-11T01:17:26.8440216Z", "2026-10-11T01:18:00.1000000Z"],  # .NET "o"
        ["2026-10-10T21:17:26.379953-04:00", "2026-10-11T03:17:27.727673+02:00"],  # Python, local
        ["2026-10-11T01:17:28.087060570Z"],  # Go, nanoseconds
        ["2026-03-29T01:00:00.000000+00:00"],
        ["2026-10-11T01:17:28Z"],
        ["2026-10-11 01:17:28"],  # naive, space separator
        ["2026-10-11T01:17"],  # no seconds
        ["2026-10-11T01:17:28+0200"],
        ["2026-10-11T01:17:28+02"],
        [
            "2026-10-11T01:17:28.5Z",
            "2026-10-11T01:17:28.25Z",
            "2026-10-11T01:17:28Z",
        ],  # JS-like trim
        ["2026-10-11T01:17:28.123456", "2026-10-11T01:17:29"],  # Python isoformat omits zero micros
        [
            "2026-10-11T01:17:28.123Z",
            "2026-10-11T01:17:28.123+05:30",
        ],  # "Z" for UTC, offset otherwise
        ["2026-10-10"],
        ["21:17:26.379953", "08:00:00.000000"],
        ["21:17"],
        ["Sun, 11 Oct 2026 01:17:28 GMT"],
        ["10/11/2026 01:17:28"],
        ["11 Oct 2026"],
        ["20261011T011728Z"],
    ],
)
def test_inferred_formats_round_trip_samples(samples: list[str]) -> None:
    fmt = infer_time_format(samples)
    assert fmt is not None, samples
    for sample in samples:
        if fmt.kind == "time":
            continue  # time-of-day round trip is checked by inference itself
        offset = iso_offset(sample) if fmt.kind == "iso" else None
        zone = timezone(offset) if offset is not None else UTC
        assert fmt.render(fmt.parse(sample, zone), zone) == sample


def test_dotnet_format_details() -> None:
    fmt = infer_time_format(["2026-10-11T01:17:26.8440216Z"])
    assert fmt == TimeFormat(kind="iso", fraction_digits=7, tz_style="Z")
    assert fmt.render(INSTANT) == "2026-10-11T01:17:26.8440216Z"
    # Non-UTC zones fall back to a numeric offset, like .NET's round-trip format.
    assert fmt.render(INSTANT, ZoneInfo("America/New_York")) == "2026-10-10T21:17:26.8440216-04:00"


def test_trim_and_omit_modes() -> None:
    trim = TimeFormat(kind="iso", fraction_digits=3, fraction_mode="trim")
    assert trim.render(INSTANT) == "2026-10-11T01:17:26.844Z"
    whole = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp()) * NS_PER_SECOND
    assert trim.render(whole) == "2026-01-01T00:00:00Z"
    assert trim.render(whole + 500_000_000) == "2026-01-01T00:00:00.5Z"
    omit = TimeFormat(kind="iso", fraction_digits=6, fraction_mode="omit_if_zero", tz_style="none")
    assert omit.render(whole) == "2026-01-01T00:00:00"
    assert omit.render(whole + 1000) == "2026-01-01T00:00:00.000001"


def test_offset_styles() -> None:
    plus_two = timezone(timedelta(hours=2))
    india = timezone(timedelta(hours=5, minutes=30))
    assert _text(TimeFormat(kind="iso", tz_style="+HHMM").render(INSTANT, plus_two)).endswith(
        "+0200"
    )
    assert _text(TimeFormat(kind="iso", tz_style="+HH").render(INSTANT, plus_two)).endswith("+02")
    assert _text(TimeFormat(kind="iso", tz_style="+HH").render(INSTANT, india)).endswith("+05:30")
    assert _text(TimeFormat(kind="iso", tz_style="z").render(INSTANT)).endswith("z")
    assert _text(TimeFormat(kind="iso", tz_style="+HH:MM").render(INSTANT)).endswith("+00:00")
    negative = timezone(-timedelta(hours=3, minutes=30))
    assert _text(TimeFormat(kind="iso", tz_style="+HH:MM").render(INSTANT, negative)).endswith(
        "-03:30"
    )


def test_date_time_and_http_date_render_in_zone() -> None:
    new_york = ZoneInfo("America/New_York")
    assert TimeFormat(kind="date").render(INSTANT) == "2026-10-11"
    assert TimeFormat(kind="date").render(INSTANT, new_york) == "2026-10-10"
    assert TimeFormat(kind="time", fraction_digits=6).render(INSTANT, new_york) == "21:17:26.844021"
    assert TimeFormat(kind="time", seconds=False).render(INSTANT) == "01:17"
    assert TimeFormat(kind="http_date").render(INSTANT, new_york) == "Sun, 11 Oct 2026 01:17:26 GMT"
    assert TimeFormat(kind="strftime", pattern="%d/%m/%Y").render(INSTANT) == "11/10/2026"


@pytest.mark.parametrize(
    ("sample", "unit", "decimals", "as_string"),
    [
        (1791681447, "s", 0, False),
        (1791681447123, "ms", 0, False),
        (1791681447123456, "us", 0, False),
        (1791681447123456789, "ns", 0, False),
        (JsonFloat("1791681447.120"), "s", 3, False),
        ("1791681447", "s", 0, True),
        ("1791681447.5", "s", 1, True),
    ],
)
def test_epoch_inference(sample: JsonValue, unit: str, decimals: int, as_string: bool) -> None:
    fmt = infer_time_format([sample])
    assert fmt is not None
    assert (fmt.kind, fmt.epoch_unit, fmt.epoch_decimals, fmt.as_string) == (
        "epoch",
        unit,
        decimals,
        as_string,
    )


def test_epoch_render_and_parse() -> None:
    seconds = TimeFormat(kind="epoch")
    assert seconds.render(INSTANT) == 1791681446
    millis = TimeFormat(kind="epoch", epoch_unit="ms", as_string=True)
    assert millis.render(INSTANT) == "1791681446844"
    decimal = TimeFormat(kind="epoch", epoch_decimals=3)
    rendered = decimal.render(INSTANT)
    assert isinstance(rendered, JsonFloat)
    assert rendered.lexeme == "1791681446.844"
    assert (
        TimeFormat(kind="epoch", epoch_decimals=2, as_string=True).render(INSTANT)
        == "1791681446.84"
    )
    assert decimal.parse(JsonFloat("1791681446.844")) == 1791681446 * NS_PER_SECOND + 844_000_000
    assert millis.parse("1791681446844") == 1791681446844 * 1_000_000
    with pytest.raises(TypeError, match="not an epoch"):
        seconds.parse(True)  # noqa: FBT003
    with pytest.raises(ValueError, match="not an epoch"):
        seconds.parse("abc")


@pytest.mark.parametrize(
    "samples",
    [
        [],
        [None],
        ["hello"],
        [5],
        [True],
        [{"a": 1}],
        ["2026-10-11", "2026-10-11T01:00:00Z"],  # mixed date/datetime
        ["2026-10-11T01:00:00Z", "2026-10-11 01:00:00Z"],  # mixed separators
        ["2026-10-11T01:00:00Z", "2026-10-11T01:00:00+0000"],  # mixed offset spellings
        ["2026-10-11T01:00:00Z", 1791681447],
        ["99999999999999999999"],
        [10**400],  # beyond float range: must not raise OverflowError
    ],
)
def test_non_time_samples_are_rejected(samples: list[JsonValue]) -> None:
    assert infer_time_format(samples) is None


def test_parse_errors_and_helpers() -> None:
    with pytest.raises(ValueError, match="ISO"):
        parse_iso("yesterday")
    with pytest.raises(TypeError, match="string timestamp"):
        TimeFormat(kind="iso").parse(5)
    with pytest.raises(ValueError, match="time of day"):
        TimeFormat(kind="time").parse("noon")
    assert TimeFormat(kind="time").parse("01:00:01.5") == 3601 * NS_PER_SECOND + 500_000_000
    assert (
        TimeFormat(kind="http_date").parse("Sun, 11 Oct 2026 01:17:26 GMT") == INSTANT - 844_021_600
    )
    naive = TimeFormat(kind="strftime", pattern="%Y/%m/%d %H:%M:%S")
    assert naive.parse("2026/10/11 01:17:26") == INSTANT - 844_021_600
    aware = TimeFormat(kind="strftime", pattern="%Y-%m-%d %H:%M:%S%z")
    assert aware.parse("2026-10-11 03:17:26+0200") == INSTANT - 844_021_600
    assert parse_iso("2026-10-10", ZoneInfo("America/New_York")) == parse_iso(
        "2026-10-10T04:00:00Z"
    )
    assert iso_offset("2026-10-10T21:17:26-04:00") == -timedelta(hours=4)
    assert iso_offset("2026-10-10T21:17:26") is None
    assert iso_offset("garbage") is None
    assert looks_like_time("2026-10-10")
    assert not looks_like_time("2026")
