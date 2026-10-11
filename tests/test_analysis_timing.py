from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fauxcall.analysis import timing
from fauxcall.analysis.timing import TimeFieldEvidence, evidence_table, time_fields
from fauxcall.clock import datetime_to_ns
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange
from fauxcall.timefmt import NS_PER_SECOND, TimeFormat
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pytest

    from fauxcall.types import JsonValue

# Friday 2026-10-09 15:00:00 UTC: a whole hour, so minute/hour snapping is easy to reason about.
BASE_NS = datetime_to_ns(datetime(2026, 10, 9, 15, 0, tzinfo=UTC))
ISO6 = TimeFormat(kind="iso", fraction_digits=6)
ISO0 = TimeFormat(kind="iso")
OFFSETS = (0.0, 20.0, 40.0, 60.0, 80.0)


def at(seconds: float) -> int:
    return BASE_NS + round(seconds * NS_PER_SECOND)


def exchange(
    number: int,
    body: JsonValue,
    *,
    sent_s: float,
    elapsed_ms: float = 0.0,
    kind: str = "json",
    response: bool = True,
) -> Exchange:
    started_ns = at(sent_s)
    captured = (
        CapturedResponse(
            status=200,
            body=Body(kind="json", json_value=body)
            if kind == "json"
            else Body(kind="text", text=str(body)),
        )
        if response
        else None
    )
    return Exchange(
        id=f"{number:06d}",
        endpoint="GET /v1/quote",
        purpose="time_profile",
        started_at=datetime.fromtimestamp(started_ns / NS_PER_SECOND, UTC),
        started_ns=started_ns,
        elapsed_ms=elapsed_ms,
        request=CapturedRequest(
            method=HttpMethod.GET, url="https://api.test/v1/quote", path="/v1/quote"
        ),
        response=captured,
    )


def series(
    value_at: Callable[[float], JsonValue],
    *,
    offsets: Sequence[float] = OFFSETS,
    elapsed_ms: float = 0.0,
    field: str = "t",
) -> list[Exchange]:
    """One exchange per offset whose body is ``{field: value_at(response time in seconds)}``."""
    half = elapsed_ms / 2000
    return [
        exchange(i, {field: value_at(offset + half)}, sent_s=offset, elapsed_ms=elapsed_ms)
        for i, offset in enumerate(offsets, start=1)
    ]


def only(evidence: list[TimeFieldEvidence]) -> TimeFieldEvidence:
    assert len(evidence) == 1, evidence
    return evidence[0]


def test_static_value_far_from_now() -> None:
    fixed = "2026-10-01T00:00:00Z"
    item = only(time_fields(series(lambda _: fixed)))
    assert item.path == "$.t"
    assert item.format == ISO0
    assert item.samples == 5
    assert item.span_seconds == 80.0
    assert item.changed is False
    assert item.suggestion == "static"
    assert item.detail == "constant over 80s"
    assert item.examples == [fixed]
    assert item.delta_spread_s == 80.0


def test_static_value_close_to_now_hints_at_a_closed_market() -> None:
    frozen = ISO0.render(at(40))
    item = only(time_fields(series(lambda _: frozen)))
    assert item.suggestion == "static"
    assert item.delta_mean_s == 0.0
    assert item.detail == "constant, but close to now — frozen by a closed market?"


def test_constant_samples_too_close_together_are_unknown() -> None:
    item = only(time_fields(series(lambda _: "2026-10-01T00:00:00Z", offsets=(0, 1, 2, 3))))
    assert item.suggestion == "unknown"
    assert item.detail == "samples too close together to tell"
    assert item.span_seconds == 3.0


def test_now_with_lag() -> None:
    evidence = time_fields(series(lambda s: ISO6.render(at(s - 0.25)), elapsed_ms=100))
    item = only(evidence)
    assert item.format == ISO6
    assert item.changed is True
    assert item.suggestion == "now"
    assert item.delta_mean_s == -0.25
    assert item.delta_spread_s == 0.0
    assert item.detail == "tracks the clock (trails by 0.250s on average)"
    assert len(item.examples) == 3


def test_now_running_ahead_of_the_clock() -> None:
    item = only(time_fields(series(lambda s: ISO6.render(at(s + 1.5)))))
    assert item.suggestion == "now"
    assert item.detail == "tracks the clock (runs ahead by 1.500s on average)"


def test_relative_keeps_its_distance_to_now() -> None:
    item = only(time_fields(series(lambda s: ISO0.render(at(s + 3600)), field="expires_at")))
    assert item.path == "$.expires_at"
    assert item.suggestion == "relative"
    assert item.delta_mean_s == 3600.0
    assert item.detail == "keeps a constant distance of +3600.0s from now"


def test_periodic_epoch_seconds_snapped_to_minutes() -> None:
    def minute(seconds: float) -> JsonValue:
        return (at(seconds) // NS_PER_SECOND) // 60 * 60

    item = only(time_fields(series(minute, offsets=(0, 50, 100, 150, 200))))
    assert item.format.kind == "epoch"
    assert item.suggestion == "periodic"
    assert item.detail == "snaps to multiples of 60s"
    assert all(isinstance(example, int) for example in item.examples)


def test_periodic_prefers_the_longest_period() -> None:
    hours = [at(0) // NS_PER_SECOND, at(3600) // NS_PER_SECOND, at(3 * 3600) // NS_PER_SECOND]
    exchanges = [
        exchange(i, {"t": value}, sent_s=sent)
        for i, (value, sent) in enumerate(zip(hours, (0, 100, 200), strict=True), start=1)
    ]
    assert only(time_fields(exchanges)).detail == "snaps to multiples of 3600s"


def test_date_near_today_that_did_not_change_is_a_calendar_candidate() -> None:
    item = only(time_fields(series(lambda _: "2026-10-09", field="as_of")))
    assert item.format.kind == "date"
    assert item.suggestion == "calendar?"
    assert item.detail.startswith("a date near today")


def test_old_constant_date_is_static() -> None:
    item = only(time_fields(series(lambda _: "2020-01-02")))
    assert item.suggestion == "static"


def test_changing_but_not_in_step_with_the_clock() -> None:
    jumps = {0.0: -100.5, 20.0: -3.25, 40.0: 7.75, 60.0: -42.125, 80.0: 1.0}
    item = only(time_fields(series(lambda s: ISO6.render(at(s + jumps[s])))))
    assert item.suggestion == "changing"
    assert item.detail == "changes, but not in step with the clock (volatile or event-driven)"


def test_response_time_is_halfway_through_the_exchange() -> None:
    evidence = time_fields(series(lambda s: ISO6.render(at(s)), elapsed_ms=4000))
    assert only(evidence).delta_mean_s == 0.0


def test_scope_and_first_list_item_per_sample() -> None:
    exchanges = [
        exchange(
            i,
            {
                "meta": {"generated": ISO0.render(at(offset))},
                "data": {
                    "items": [
                        {"t": ISO0.render(at(offset - 1))},
                        {"t": "2020-01-01T00:00:00Z"},
                    ]
                },
            },
            sent_s=offset,
        )
        for i, offset in enumerate(OFFSETS, start=1)
    ]
    everything = {item.path: item for item in time_fields(exchanges)}
    assert set(everything) == {"$.meta.generated", "$.data.items[*].t"}
    first_items = everything["$.data.items[*].t"]
    assert first_items.suggestion == "now"
    assert "2020-01-01T00:00:00Z" not in first_items.examples
    scoped = only(time_fields(exchanges, scope="$.data"))
    assert scoped.path == "$.items[*].t"
    assert time_fields(exchanges, scope="$.nowhere") == []


def test_unusable_samples_and_fields_are_skipped() -> None:
    good = series(lambda s: ISO0.render(at(s)))
    noise = [
        exchange(90, "not json", sent_s=5, kind="text"),
        exchange(91, None, sent_s=6, response=False),
        exchange(92, None, sent_s=7),
    ]
    bodies: list[JsonValue] = [
        {"symbol": "AAPL", "open": "09:30", "flag": True, "gone": None, "once": 1, "mixed": 1},
        {"symbol": "MSFT", "open": "09:30", "flag": False, "gone": None, "mixed": "x"},
    ]
    other = [exchange(80 + i, body, sent_s=i * 30) for i, body in enumerate(bodies)]
    evidence = time_fields([*good, *noise, *other])
    assert [item.path for item in evidence] == ["$.t"]
    assert evidence[0].samples == 5


def test_values_that_fail_to_parse_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timing, "infer_time_format", lambda _values: TimeFormat(kind="epoch"))
    assert time_fields(series(lambda _: "not-a-number")) == []


def test_examples_are_distinct_raw_values_in_order() -> None:
    values = iter([1_791_000_000, 1_791_000_000, 1_791_000_060, 1_791_000_120, 1_791_000_180])
    item = only(time_fields(series(lambda _: next(values))))
    assert item.examples == [1_791_000_000, 1_791_000_060, 1_791_000_120]


def test_evidence_table() -> None:
    iso = only(time_fields(series(lambda s: ISO6.render(at(s - 0.25)))))
    epoch = only(time_fields(series(lambda s: at(s) // NS_PER_SECOND, field="ts")))
    table = evidence_table([iso, epoch]).splitlines()
    assert table[0] == (
        "| field | format | samples | span | changed | delta mean | spread | suggestion |"
    )
    assert table[1] == "|---|---|---|---|---|---|---|---|"
    assert table[2] == (
        "| `$.t` | iso 6f Z | 5 | 80s | yes | -0.25 | 0.0 | "
        "**now** — tracks the clock (trails by 0.250s on average) |"
    )
    assert table[3].startswith("| `$.ts` | epoch | 5 | 80s | yes | 0.0 | 0.0 | **now**")
    assert evidence_table([]) == "\n".join(table[:2])
