from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from fauxcall import jsonpath
from fauxcall.calendar import TradingCalendar
from fauxcall.clock import datetime_to_ns
from fauxcall.jsonfmt import JsonFloat
from fauxcall.model.calendar import CalendarModel, SessionWindow
from fauxcall.model.fields import (
    CalendarPolicy,
    ComputedPolicy,
    CounterPolicy,
    EchoPolicy,
    FieldPolicy,
    IgnorePolicy,
    NowPolicy,
    PeriodicPolicy,
    RandomPolicy,
    RelativePolicy,
    SeriesPolicy,
    StaticPolicy,
    VolatilePolicy,
)
from fauxcall.runtime.policies import apply_policies
from fauxcall.timefmt import TimeFormat, parse_iso

if TYPE_CHECKING:
    from fauxcall.model.common import ZoneSpec
    from fauxcall.types import JsonValue

ISO = TimeFormat(kind="iso")
DATE = TimeFormat(kind="date")
OPEN = datetime_to_ns(datetime(2026, 10, 13, 14, 30, tzinfo=UTC))  # Tuesday 10:30 New York
SATURDAY = datetime_to_ns(datetime(2026, 10, 10, 12, 0, tzinfo=UTC))
NYSE = TradingCalendar(
    CalendarModel(
        name="nyse",
        zone="America/New_York",
        sessions=[SessionWindow(open=time(9, 30), close=time(16))],
    )
)
ECB = TradingCalendar(CalendarModel(name="ecb", zone="Europe/Berlin", publications=[time(16)]))
EMPTY = TradingCalendar(CalendarModel(name="empty", zone="UTC", weekdays=[]))


@dataclass
class Env:
    now_ns: int = OPEN
    rng: random.Random = field(default_factory=lambda: random.Random(1))
    request: dict[str, JsonValue] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    series: dict[str, JsonValue] = field(default_factory=dict)

    def zone(self, spec: ZoneSpec, document: JsonValue) -> tzinfo:
        name: JsonValue = spec
        if spec.startswith("param:"):
            name = self.request.get(spec)
        elif spec.startswith("field:"):
            name = jsonpath.get_one(document, spec.removeprefix("field:"))
        return (
            ZoneInfo(str(name)) if isinstance(name, str) and name != "calendar" else ZoneInfo("UTC")
        )

    def calendar(self, name: str) -> TradingCalendar:
        return {"nyse": NYSE, "ecb": ECB, "empty": EMPTY}[name]

    def next_counter(self, key: str, start: int, step: int) -> int:
        value = self.counters.get(key, start - step) + step
        self.counters[key] = value
        return value

    def series_value(self, name: str, mode: str) -> JsonValue:
        del mode
        return self.series.get(name)

    def request_value(self, source: str) -> JsonValue:
        return self.request.get(source)


def run(
    value: Any,
    *policies: FieldPolicy,
    env: Env | None = None,
    anchor: int | None = None,
    scope: str = "record",
) -> JsonValue:
    return apply_policies(value, list(policies), env or Env(), scope=scope, anchor_ns=anchor)  # type: ignore[arg-type]


def test_no_policies_returns_the_same_object_and_inputs_are_never_mutated() -> None:
    record: JsonValue = {"a": 1}
    assert run(record) is record
    original: JsonValue = {"t": "2020-01-01T00:00:00Z"}
    result = run(original, NowPolicy(path="t", format=ISO))
    assert original == {"t": "2020-01-01T00:00:00Z"}
    assert result == {"t": "2026-10-13T14:30:00Z"}


def test_now_with_lag_and_zones() -> None:
    env = Env(request={"param:tz": "Asia/Tokyo"})
    body = {"utc": "", "local": "", "field": "", "zone": "Europe/Paris", "bad": ""}
    result = run(
        body,
        NowPolicy(path="utc", format=ISO, lag_ms=1500),
        NowPolicy(path="local", format=TimeFormat(kind="iso", tz_style="+HH:MM"), zone="param:tz"),
        NowPolicy(
            path="field", format=TimeFormat(kind="iso", tz_style="+HH:MM"), zone="field:zone"
        ),
        env=env,
    )
    assert isinstance(result, dict)
    assert result["utc"] == "2026-10-13T14:29:58Z"
    assert result["local"] == "2026-10-13T23:30:00+09:00"
    assert result["field"] == "2026-10-13T16:30:00+02:00"


def test_relative_keeps_the_distance_to_now() -> None:
    recorded_at = datetime_to_ns(datetime(2026, 10, 9, 15, 0, tzinfo=UTC))
    record = {"due": "2026-10-09T16:00:00Z", "junk": "not a time"}
    result = run(
        record,
        RelativePolicy(path="due", format=ISO),
        RelativePolicy(path="junk", format=ISO),
        anchor=recorded_at,
    )
    assert isinstance(result, dict)
    assert result["due"] == "2026-10-13T15:30:00Z"  # now + 1h, like when recorded
    assert result["junk"] == "not a time"
    assert run(record, RelativePolicy(path="due", format=ISO)) == record  # no anchor: unchanged


@pytest.mark.parametrize(
    ("event", "now", "expected"),
    [
        ("last_tick", OPEN, "2026-10-13T14:29:59Z"),
        ("last_tick", SATURDAY, "2026-10-09T20:00:00Z"),
        ("last_close", OPEN, "2026-10-12T20:00:00Z"),
        ("last_open", OPEN, "2026-10-13T13:30:00Z"),
    ],
)
def test_calendar_events(event: str, now: int, expected: str) -> None:
    policy = CalendarPolicy.model_validate(
        {"path": "t", "calendar": "nyse", "event": event, "lag_ms": 1000, "format": {"kind": "iso"}}
    )
    assert run({"t": "old"}, policy, env=Env(now_ns=now)) == {"t": expected}


def test_publication_and_session_dates_render_in_the_calendar_zone() -> None:
    publication = CalendarPolicy(
        path="date", calendar="ecb", event="last_publication", zone="calendar", format=DATE
    )
    assert run({"date": "x"}, publication, env=Env(now_ns=SATURDAY)) == {"date": "2026-10-09"}
    session = CalendarPolicy(path="day", calendar="nyse", event="session_date", format=DATE)
    late = datetime_to_ns(datetime(2026, 10, 10, 3, 30, tzinfo=UTC))  # still Friday in New York
    assert run({"day": "x"}, session, env=Env(now_ns=late)) == {"day": "2026-10-09"}
    nothing = CalendarPolicy(path="day", calendar="empty", event="last_close", format=DATE)
    assert run({"day": "keep"}, nothing) == {"day": "keep"}
    no_session = CalendarPolicy(path="day", calendar="empty", event="session_date", format=DATE)
    assert run({"day": "keep"}, no_session) == {"day": "keep"}


def test_periodic_snaps_to_the_period() -> None:
    policy = PeriodicPolicy(path="t", period_seconds=300, phase_seconds=60, format=ISO)
    later = OPEN + 7 * 60 * 1_000_000_000
    assert run({"t": ""}, policy, env=Env(now_ns=later)) == {"t": "2026-10-13T14:36:00Z"}


def test_random_generators() -> None:
    policies = [
        RandomPolicy(path="u4", generator="uuid4"),
        RandomPolicy(path="u7", generator="uuid7"),
        RandomPolicy(path="hx", generator="hex", length=8),
        RandomPolicy(path="tp", generator="traceparent"),
        RandomPolicy(path="dg", generator="digits", length=6, prefix="ORD-"),
        RandomPolicy(path="dn", generator="digits", length=6, as_number=True),
        RandomPolicy(path="dp", generator="digits", length=3, as_number=True, prefix="9"),
        RandomPolicy(path="an", generator="alnum", length=5),
        RandomPolicy(path="b6", generator="base64url", length=10),
        RandomPolicy(path="ch", generator="choice", choices=["a", "b"]),
    ]
    result = run({p.path: None for p in policies}, *policies)
    assert isinstance(result, dict)
    assert len(str(result["u4"])) == 36
    assert str(result["u7"])[14] == "7"
    assert len(str(result["hx"])) == 8
    assert str(result["tp"]).startswith("00-")
    assert str(result["dg"]).startswith("ORD-")
    assert isinstance(result["dn"], int)
    assert isinstance(result["dp"], int)
    assert str(result["dp"]).startswith("9")
    assert len(str(result["an"])) == 5
    assert len(str(result["b6"])) == 10
    assert result["ch"] in {"a", "b"}
    # Seeded: the same request seed gives the same values.
    assert run({p.path: None for p in policies}, *policies) == result


def test_counter_echo_series_and_passthrough_policies() -> None:
    env = Env(
        request={
            "query.amount": "10",
            "query.base": "usd",
            "query.flag": "yes",
            "query.bad": "x",
            "body.name": "Ann",
        },
        series={"price": JsonFloat("101.25")},
    )
    record = {
        "seq": 0,
        "seq_s": "",
        "amount": None,
        "base": None,
        "flag": None,
        "bad": None,
        "lower": None,
        "f": None,
        "missing": "orig",
        "dflt": "orig",
        "name": None,
        "price": 1,
        "unknown": 2,
        "s": "st",
        "v": "vo",
        "i": "ig",
        "c": "co",
    }
    policies: list[FieldPolicy] = [
        CounterPolicy(path="seq", start=100, step=5),
        CounterPolicy(path="seq_s", as_string=True),
        EchoPolicy(path="amount", source="query.amount", transform="int"),
        EchoPolicy(path="base", source="query.base", transform="upper"),
        EchoPolicy(path="flag", source="query.flag", transform="bool"),
        EchoPolicy(path="bad", source="query.bad", transform="float"),
        EchoPolicy(path="lower", source="query.base", transform="lower"),
        EchoPolicy(path="f", source="query.amount", transform="float"),
        EchoPolicy(path="missing", source="query.nope"),
        EchoPolicy(path="dflt", source="query.nope", default="fallback"),
        EchoPolicy(path="name", source="body.name"),
        SeriesPolicy(path="price", series="price"),
        SeriesPolicy(path="unknown", series="nope"),
        StaticPolicy(path="s"),
        VolatilePolicy(path="v"),
        IgnorePolicy(path="i"),
        ComputedPolicy(path="c", function="x"),
    ]
    result = run(record, *policies, env=env)
    assert isinstance(result, dict)
    assert (result["seq"], result["seq_s"]) == (100, "1")
    assert (result["amount"], result["base"], result["flag"], result["bad"]) == (
        10,
        "USD",
        True,
        "x",
    )
    assert (result["lower"], result["f"]) == ("usd", 10.0)
    assert (result["missing"], result["dflt"], result["name"]) == ("orig", "fallback", "Ann")
    assert result["price"] == JsonFloat("101.25")
    assert result["unknown"] == 2
    assert [result[k] for k in ("s", "v", "i", "c")] == ["st", "vo", "ig", "co"]
    again = run(record, CounterPolicy(path="seq", start=100, step=5), env=env)
    assert isinstance(again, dict)
    assert again["seq"] == 105


def test_scopes_root_paths_and_arrays() -> None:
    body = {"items": [{"t": ""}, {"t": ""}]}
    body_policy = NowPolicy(path="items[*].t", scope="body", format=ISO)
    assert run(body, body_policy, scope="record") == body  # other scope: untouched
    result = run(body, body_policy, scope="body")
    assert result == {"items": [{"t": "2026-10-13T14:30:00Z"}, {"t": "2026-10-13T14:30:00Z"}]}
    root = run("2000-01-01T00:00:00Z", NowPolicy(path="$", scope="body", format=ISO), scope="body")
    assert root == "2026-10-13T14:30:00Z"
    assert parse_iso(str(root)) == OPEN


def test_echo_transforms_handle_none_and_bad_values() -> None:
    env = Env(request={"query.n": None})
    assert run({"n": "keep"}, EchoPolicy(path="n", source="query.n", transform="int"), env=env) == {
        "n": "keep"
    }
    weird = Env(request={"query.n": "abc"})
    assert run({"n": 0}, EchoPolicy(path="n", source="query.n", transform="int"), env=weird) == {
        "n": "abc"
    }


def test_date_rendering_helper_respects_zone() -> None:
    session = CalendarPolicy(
        path="d", calendar="nyse", event="session_date", format=TimeFormat(kind="iso")
    )
    rendered = run({"d": ""}, session)
    assert rendered == {"d": "2026-10-13T00:00:00-04:00"}
    assert date(2026, 10, 13).isoformat() in str(rendered)
