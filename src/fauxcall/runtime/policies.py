"""Apply field policies to records and bodies at response time.

This is where "``GET current_time`` returns the actual time" and "a ticker's ``time`` tracks now
during market hours but stays at the last tick overnight" happen. Policies never mutate world data:
they work on a copy.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Protocol

from fauxcall import jsonpath
from fauxcall.model.fields import (
    CalendarPolicy,
    CounterPolicy,
    EchoPolicy,
    NowPolicy,
    PeriodicPolicy,
    RandomPolicy,
    RelativePolicy,
    SeriesPolicy,
)
from fauxcall.runtime import generators
from fauxcall.timefmt import NS_PER_SECOND, TimeFormat

if TYPE_CHECKING:
    import random
    from collections.abc import Callable, Iterable
    from datetime import date

    from fauxcall.calendar import TradingCalendar
    from fauxcall.model.common import ZoneSpec
    from fauxcall.model.fields import FieldPolicy, PolicyScope
    from fauxcall.types import JsonValue


class PolicyEnvironment(Protocol):
    """What policies need from the current request."""

    @property
    def now_ns(self) -> int:
        """The fake clock's current instant for this request (captured once)."""
        ...

    @property
    def rng(self) -> random.Random:
        """Request-seeded random generator."""
        ...

    def zone(self, spec: ZoneSpec, document: JsonValue) -> tzinfo:
        """Resolve a zone spec (IANA name, ``param:<name>``, ``field:<path>``)."""
        ...

    def calendar(self, name: str) -> TradingCalendar:
        """The named calendar."""
        ...

    def next_counter(self, key: str, start: int, step: int) -> int:
        """Next value of a fake-wide counter."""
        ...

    def series_value(self, name: str, mode: str) -> JsonValue:
        """Current value of a recorded series (``None`` if unknown)."""
        ...

    def request_value(self, source: str) -> JsonValue:
        """``query.<n>``, ``path.<n>``, ``header.<n>`` or ``body.<path>`` from the request."""
        ...


def _transform(value: JsonValue, transform: str) -> JsonValue:
    if value is None or transform == "as_is":
        return value
    text = str(value)
    try:
        match transform:
            case "int":
                return int(float(text))
            case "float":
                return float(text)
            case "upper":
                return text.upper()
            case "lower":
                return text.lower()
            case "bool":
                return text.strip().lower() in {"1", "true", "yes", "on"}
    except ValueError:
        return value
    return value  # pragma: no cover - EchoTransform is a closed Literal


def _date_to_ns(day: date, zone: tzinfo) -> int:
    midnight = datetime(day.year, day.month, day.day, tzinfo=zone)
    return int(midnight.astimezone(UTC).timestamp()) * NS_PER_SECOND


def _calendar_instant(policy: CalendarPolicy, env: PolicyEnvironment) -> tuple[int, tzinfo] | None:
    calendar = env.calendar(policy.calendar)
    now = env.now_ns
    if policy.event == "session_date":
        day = calendar.session_date(now)
        return None if day is None else (_date_to_ns(day, calendar.zone), calendar.zone)
    events: dict[str, Callable[[], int | None]] = {
        "last_tick": lambda: calendar.last_tick(now, policy.lag_ms * 1_000_000),
        "last_close": lambda: calendar.last_close(now),
        "last_open": lambda: calendar.last_open(now),
        "last_publication": lambda: calendar.last_publication(now),
    }
    instant = events[policy.event]()
    return None if instant is None else (instant, calendar.zone)


def _render_time(fmt: TimeFormat, instant: int, zone: tzinfo) -> JsonValue:
    return fmt.render(instant, zone)


def _new_value(  # noqa: C901, PLR0912 - one case per policy kind keeps this readable
    policy: FieldPolicy,
    old: JsonValue,
    env: PolicyEnvironment,
    *,
    anchor_ns: int | None,
    document: JsonValue,
) -> JsonValue:
    match policy:
        case NowPolicy():
            zone = env.zone(policy.zone, document)
            return _render_time(policy.format, env.now_ns - policy.lag_ms * 1_000_000, zone)
        case RelativePolicy():
            if anchor_ns is None:
                return old
            zone = env.zone(policy.zone, document)
            try:
                recorded = policy.format.parse(old, zone)
            except (ValueError, TypeError):
                return old
            return _render_time(policy.format, recorded + env.now_ns - anchor_ns, zone)
        case CalendarPolicy():
            found = _calendar_instant(policy, env)
            if found is None:
                return old
            instant, calendar_zone = found
            use_calendar_zone = policy.zone == "calendar" or policy.event == "session_date"
            zone = calendar_zone if use_calendar_zone else env.zone(policy.zone, document)
            return _render_time(policy.format, instant, zone)
        case PeriodicPolicy():
            zone = env.zone(policy.zone, document)
            period = int(policy.period_seconds * NS_PER_SECOND)
            phase = int(policy.phase_seconds * NS_PER_SECOND)
            snapped = (env.now_ns - phase) // period * period + phase
            return _render_time(policy.format, snapped, zone)
        case RandomPolicy():
            return _random_value(policy, env)
        case CounterPolicy():
            value = env.next_counter(policy.path, policy.start, policy.step)
            return str(value) if policy.as_string else value
        case EchoPolicy():
            echoed = _transform(env.request_value(policy.source), policy.transform)
            if echoed is None:
                return policy.default if policy.default is not None else old
            return echoed
        case SeriesPolicy():
            replayed = env.series_value(policy.series, policy.mode)
            return old if replayed is None else replayed
        case _:
            return old


def _random_value(policy: RandomPolicy, env: PolicyEnvironment) -> JsonValue:
    rng = env.rng
    length = policy.length
    if policy.generator == "choice":
        return generators.choice(rng, policy.choices)
    makers: dict[str, Callable[[], str]] = {
        "uuid4": lambda: generators.uuid4(rng),
        "uuid7": lambda: generators.uuid7(rng, env.now_ns),
        "hex": lambda: generators.hex_string(rng, length or 32),
        "traceparent": lambda: generators.traceparent(rng),
        "digits": lambda: generators.digits(rng, length or 8),
        "alnum": lambda: generators.alnum(rng, length or 16),
        "base64url": lambda: generators.base64url(rng, length or 22),
    }
    text = makers[policy.generator]()
    if policy.generator == "digits" and policy.as_number:
        return int(policy.prefix + text) if policy.prefix.isdigit() else int(text)
    return policy.prefix + text


def apply_policies(
    value: JsonValue,
    policies: Iterable[FieldPolicy],
    env: PolicyEnvironment,
    *,
    scope: PolicyScope,
    anchor_ns: int | None = None,
) -> JsonValue:
    """Return a copy of ``value`` with every policy of ``scope`` applied.

    Args:
        value: A record (``scope="record"``) or a whole response body (``scope="body"``).
        policies: The endpoint's field policies.
        env: The current request.
        scope: Which policies to apply.
        anchor_ns: When ``value`` was recorded; anchors ``relative`` policies.
    """
    selected = [policy for policy in policies if policy.scope == scope]
    if not selected:
        return value
    document = value
    result = copy.deepcopy(value)
    for policy in selected:
        if not jsonpath.parse(policy.path):
            result = _new_value(policy, result, env, anchor_ns=anchor_ns, document=document)
            continue
        jsonpath.set_all(
            result,
            policy.path,
            lambda old, policy=policy: _new_value(  # type: ignore[misc]
                policy, old, env, anchor_ns=anchor_ns, document=document
            ),
        )
    return result
