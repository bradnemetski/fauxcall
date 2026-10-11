from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from fauxcall.jsonfmt import JsonFloat, dumps
from fauxcall.runtime.series import Series, SeriesStore

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonValue

S = 1_000_000_000
T0 = 1_790_000_000 * S  # recorded timeline
ORIGIN = 1_800_000_000 * S  # when playback started


def prices() -> Series:
    return Series([(T0 + 20 * S, "v2"), (T0, "v0"), (T0 + 10 * S, "v1")])  # unsorted on purpose


def test_a_series_needs_points() -> None:
    with pytest.raises(ValueError, match="at least one point"):
        Series([])


@pytest.mark.parametrize(
    ("elapsed_s", "expected"),
    [(-5, "v0"), (0, "v0"), (9.999, "v0"), (10, "v1"), (20, "v2"), (25, "v2"), (10_000, "v2")],
)
def test_hold_last(elapsed_s: float, expected: str) -> None:
    now = ORIGIN + round(elapsed_s * S)
    assert prices().value_at(now, origin_ns=ORIGIN, mode="hold_last") == expected


@pytest.mark.parametrize(
    ("elapsed_s", "expected"),
    [
        (0, "v0"),
        (9.999, "v0"),
        (10, "v1"),
        (20, "v2"),
        (29.999, "v2"),  # the last sample holds for one mean interval too
        (30, "v0"),
        (45, "v1"),
        (300 + 21, "v2"),
    ],
)
def test_loop_wraps_after_the_span_plus_one_interval(elapsed_s: float, expected: str) -> None:
    series = prices()
    assert series.period_ns == 30 * S
    now = ORIGIN + round(elapsed_s * S)
    assert series.value_at(now, origin_ns=ORIGIN, mode="loop") == expected


def test_unknown_modes_hold_the_last_value() -> None:
    assert prices().value_at(ORIGIN + 99 * S, origin_ns=ORIGIN, mode="other") == "v2"


def test_single_instant_series_are_constant() -> None:
    single = Series([(T0, 1)])
    assert single.period_ns == 0
    assert single.value_at(ORIGIN + 5 * S, origin_ns=ORIGIN, mode="loop") == 1
    duplicated = Series([(T0, "a"), (T0, "b")])
    assert duplicated.period_ns == 0
    assert duplicated.value_at(ORIGIN, origin_ns=ORIGIN, mode="loop") == "b"


def test_irregular_sampling_uses_the_mean_interval() -> None:
    series = Series([(0, "a"), (0, "a2"), (4 * S, "b"), (10 * S, "c")])
    assert series.period_ns == 15 * S  # span 10 s + 10 s / 2 intervals
    assert series.value_at(0, origin_ns=0, mode="loop") == "a2"
    assert series.value_at(14 * S, origin_ns=0, mode="loop") == "c"
    assert series.value_at(15 * S, origin_ns=0, mode="loop") == "a2"


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_store_loads_lazily_ignoring_bad_lines(tmp_path: Path) -> None:
    write_lines(
        tmp_path / "price.jsonl",
        [
            f'{{"t_ns": {T0}, "value": 105.50}}',
            "",
            "{not json",
            '{"value": 1}',
            '{"t_ns": true, "value": 2}',
            '{"t_ns": 1.5, "value": 3}',
            "[1, 2]",
            f'{{"t_ns": {T0 + 10 * S}, "value": {{"bid": 106.25}}}}',
        ],
    )
    store = SeriesStore(tmp_path, origin_ns=ORIGIN)
    with capture_logs() as logs:
        first: JsonValue = store.value("price", ORIGIN, "hold_last")
    assert isinstance(first, JsonFloat)
    assert dumps(first) == "105.50"
    assert store.value("price", ORIGIN + 10 * S, "hold_last") == {"bid": 106.25}
    ignored = [entry["line"] for entry in logs if entry["event"] == "series_line_ignored"]
    assert ignored == [3, 4, 5, 6, 7]


def test_store_reads_each_series_once(tmp_path: Path) -> None:
    path = tmp_path / "rate.jsonl"
    write_lines(path, [f'{{"t_ns": {T0}, "value": "first"}}'])
    store = SeriesStore(tmp_path, origin_ns=ORIGIN)
    assert store.value("rate", ORIGIN, "loop") == "first"
    write_lines(path, [f'{{"t_ns": {T0}, "value": "changed"}}'])
    assert store.value("rate", ORIGIN, "loop") == "first"


def test_unknown_or_empty_series_have_no_value(tmp_path: Path) -> None:
    write_lines(tmp_path / "empty.jsonl", ["{bad", '{"no": "instant"}'])
    store = SeriesStore(tmp_path, origin_ns=ORIGIN)
    with capture_logs():
        assert store.value("empty", ORIGIN, "loop") is None
    assert store.value("missing", ORIGIN, "loop") is None
    assert SeriesStore(tmp_path / "nowhere", origin_ns=0).value("price", 0, "loop") is None
