"""Opt-in replay of recorded value series (e.g. a price sampled every few seconds while recording).

``data/series/<name>.jsonl`` holds ``{"t_ns": <recorded instant>, "value": <value>}`` lines. The
fake maps "now" onto the recorded timeline, starting at the first sample when playback starts
(``origin``). Every sample holds until the next one; the last one holds for the mean sampling
interval in ``loop`` mode, then the series starts over (repeatable movement forever), and forever in
``hold_last`` mode. Lines that are not valid JSON or lack an integer ``t_ns`` are ignored.
"""

from __future__ import annotations

import bisect
from typing import TYPE_CHECKING

from fauxcall import jsonfmt
from fauxcall.logs import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonValue

log = get_logger(__name__)


class Series:
    """One recorded series."""

    def __init__(self, points: list[tuple[int, JsonValue]]) -> None:
        if not points:
            raise ValueError("a series needs at least one point")
        self._points = sorted(points, key=lambda point: point[0])
        self._times = [t for t, _ in self._points]
        distinct = len(set(self._times))
        span = self._times[-1] - self._times[0]
        # One loop = the recorded span plus one mean interval, so the last sample is shown too.
        self._period = span + span // (distinct - 1) if distinct > 1 else 0

    @property
    def period_ns(self) -> int:
        """Length of one loop (0 for a single-instant series)."""
        return self._period

    def value_at(self, now_ns: int, *, origin_ns: int, mode: str) -> JsonValue:
        """Value at ``now_ns`` when playback started at ``origin_ns``."""
        offset = max(0, now_ns - origin_ns)
        if mode == "loop" and self._period > 0:
            offset %= self._period
        position = min(self._times[0] + offset, self._times[-1])
        return self._points[bisect.bisect_right(self._times, position) - 1][1]


def _read_points(path: Path) -> list[tuple[int, JsonValue]]:
    points: list[tuple[int, JsonValue]] = []
    if not path.is_file():
        return points
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = jsonfmt.loads(line)
            except ValueError:
                parsed = None
            instant = parsed.get("t_ns") if isinstance(parsed, dict) else None
            if (
                isinstance(parsed, dict)
                and isinstance(instant, int)
                and not isinstance(instant, bool)
            ):
                points.append((instant, parsed.get("value")))
            else:
                log.warning("series_line_ignored", path=str(path), line=number)
    return points


class SeriesStore:
    """Lazily loaded series of one fake."""

    def __init__(self, series_dir: Path, *, origin_ns: int) -> None:
        self._dir = series_dir
        self._origin = origin_ns
        self._loaded: dict[str, Series | None] = {}

    def _series(self, name: str) -> Series | None:
        if name not in self._loaded:
            points = _read_points(self._dir / f"{name}.jsonl")
            self._loaded[name] = Series(points) if points else None
        return self._loaded[name]

    def value(self, name: str, now_ns: int, mode: str) -> JsonValue:
        """Current value of series ``name`` (``None`` when unknown)."""
        series = self._series(name)
        if series is None:
            return None
        return series.value_at(now_ns, origin_ns=self._origin, mode=mode)
