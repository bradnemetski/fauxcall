"""How timestamp fields behave over time — evidence for choosing field policies.

Record the same request several times (``purpose: time_profile``, e.g. 5 calls 20 s apart), then
compare each timestamp with when the response was produced:

* value constant while time passed            -> ``static`` (or frozen by a calendar: market closed)
* value - response time ~ constant, ~0         -> ``now`` (``lag`` = how far behind it trails)
* value - response time ~ constant, far from 0 -> ``relative`` (keeps its distance to now)
* value snaps to multiples of a period          -> ``periodic``
* a date equal to the last business/publication day -> ``calendar`` (needs domain knowledge)
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field

from fauxcall import jsonpath
from fauxcall.model.common import Model, RawJson
from fauxcall.timefmt import NS_PER_SECOND, TimeFormat, infer_time_format

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fauxcall.model.recording import Exchange
    from fauxcall.types import JsonValue

NOW_TOLERANCE_S: Final = 5.0
STABLE_DELTA_S: Final = 2.0
SECONDS_PER_DAY: Final = 86_400
_PERIODS: Final = (60, 300, 900, 3600, SECONDS_PER_DAY)
_MAX_EXAMPLES: Final = 3

Suggestion = Literal["static", "now", "relative", "periodic", "calendar?", "changing", "unknown"]


class TimeFieldEvidence(Model):
    """What the samples say about one timestamp field."""

    path: str
    format: TimeFormat
    samples: int
    span_seconds: float = Field(description="Wall-clock time covered by the samples.")
    changed: bool
    delta_mean_s: float | None = Field(description="Mean of (value - response time), seconds.")
    delta_spread_s: float | None = Field(description="Max - min of the deltas, seconds.")
    suggestion: Suggestion
    detail: str
    examples: list[RawJson] = Field(
        default_factory=list, description="Up to 3 distinct values, as they appeared."
    )


def _response_ns(exchange: Exchange) -> int:
    """When the response was (approximately) produced: halfway through the exchange."""
    return exchange.started_ns + int(exchange.elapsed_ms * 1_000_000 / 2)


def _period_of(values_ns: Sequence[int]) -> int | None:
    """The longest period (seconds) every value is a multiple of, if any."""
    for period in reversed(_PERIODS):
        if all(value % (period * NS_PER_SECOND) == 0 for value in values_ns):
            return period
    return None


def _tracking_detail(mean: float) -> str:
    if mean > 0:
        return f"tracks the clock (runs ahead by {mean:.3f}s on average)"
    return f"tracks the clock (trails by {-mean:.3f}s on average)"


def _suggest(
    values_ns: Sequence[int], times_ns: Sequence[int], fmt: TimeFormat, span_s: float
) -> tuple[Suggestion, str, float, float]:
    """Suggestion, explanation, mean and spread of ``value - response time`` (seconds)."""
    deltas = [(v - t) / NS_PER_SECOND for v, t in zip(values_ns, times_ns, strict=True)]
    mean = statistics.fmean(deltas)
    spread = max(deltas) - min(deltas)
    changed = len(set(values_ns)) > 1
    if not changed:
        if span_s < STABLE_DELTA_S * 2:
            return "unknown", "samples too close together to tell", mean, spread
        if fmt.kind == "date" and -2 * SECONDS_PER_DAY <= mean <= SECONDS_PER_DAY:
            return (
                "calendar?",
                "a date near today that didn't change: last business/publication day?",
                mean,
                spread,
            )
        if abs(mean) < NOW_TOLERANCE_S * 3 and span_s > NOW_TOLERANCE_S:
            return "static", "constant, but close to now — frozen by a closed market?", mean, spread
        return "static", f"constant over {span_s:.0f}s", mean, spread
    if spread <= max(STABLE_DELTA_S, span_s * 0.05):
        if abs(mean) <= NOW_TOLERANCE_S:
            return "now", _tracking_detail(mean), mean, spread
        return "relative", f"keeps a constant distance of {mean:+.1f}s from now", mean, spread
    period = _period_of(values_ns)
    if period is not None:
        return "periodic", f"snaps to multiples of {period}s", mean, spread
    return (
        "changing",
        "changes, but not in step with the clock (volatile or event-driven)",
        mean,
        spread,
    )


def _distinct(values: Sequence[JsonValue], limit: int) -> list[JsonValue]:
    """The first ``limit`` distinct values, in order of appearance."""
    found: list[JsonValue] = []
    for value in values:
        if value not in found:
            found.append(value)
            if len(found) == limit:
                break
    return found


def time_fields(exchanges: Sequence[Exchange], *, scope: str = "$") -> list[TimeFieldEvidence]:
    """Evidence for every timestamp-like leaf in the JSON responses of ``exchanges``.

    Only fields present in at least two samples are analysed. List items are generalized with
    ``[*]``; per sample the first item's value is used so positions stay comparable.
    """
    observed: defaultdict[str, list[tuple[int, JsonValue]]] = defaultdict(list)
    for exchange in exchanges:
        if exchange.response is None:
            continue
        body = exchange.response.body.parsed()
        if body is None:
            continue
        root = jsonpath.get_one(body, scope)
        seen_paths: set[str] = set()
        for path, value in jsonpath.leaf_paths(root):
            if path in seen_paths or value is None:
                continue
            seen_paths.add(path)
            observed[path].append((_response_ns(exchange), value))
    evidence: list[TimeFieldEvidence] = []
    for path, points in observed.items():
        if len(points) < 2:  # noqa: PLR2004 - a single sample says nothing about behavior
            continue
        values = [value for _, value in points]
        fmt = infer_time_format(values)
        if fmt is None or fmt.kind == "time":
            continue
        try:
            parsed = [fmt.parse(value) for value in values]
        except (ValueError, TypeError):
            continue
        times = [t for t, _ in points]
        span = (max(times) - min(times)) / NS_PER_SECOND
        suggestion, detail, mean, spread = _suggest(parsed, times, fmt, span)
        evidence.append(
            TimeFieldEvidence(
                path=path,
                format=fmt,
                samples=len(points),
                span_seconds=round(span, 3),
                changed=len(set(parsed)) > 1,
                delta_mean_s=round(mean, 3),
                delta_spread_s=round(spread, 3),
                suggestion=suggestion,
                detail=detail,
                examples=_distinct(values, _MAX_EXAMPLES),
            )
        )
    return evidence


def evidence_table(evidence: Sequence[TimeFieldEvidence]) -> str:
    """Markdown table for the agent."""
    lines = [
        "| field | format | samples | span | changed | delta mean | spread | suggestion |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in evidence:
        fmt = item.format
        spelled = fmt.kind if fmt.kind != "iso" else f"iso {fmt.fraction_digits}f {fmt.tz_style}"
        lines.append(
            f"| `{item.path}` | {spelled} | {item.samples} | {item.span_seconds:.0f}s | "
            f"{'yes' if item.changed else 'no'} | {item.delta_mean_s} | {item.delta_spread_s} | "
            f"**{item.suggestion}** — {item.detail} |"
        )
    return "\n".join(lines)
