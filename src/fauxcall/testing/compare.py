"""Weighted parity checks between a real and a fake response.

Each check has a weight; compliance is ``passed weight / total weight``. Field policies decide how
values are compared: static fields must match, time fields must *behave* the same (e.g. "close to
the response time" on both sides), random/volatile fields must have the same shape, pseudonymized
fields are compared through the pseudonym key.

Record-scope policies are paths inside a record. Where the records sit in a body comes from
``record_roots`` (``["$"]`` for a single record, ``["$[*]"]`` for a bare list, ``["$.data[*]"]``
for an envelope); without it the roots are guessed from the real body: every element of a bare
list; else the body itself when the field is there; else the elements of top-level lists of
objects; else the top-level objects holding the field. So a record policy for ``id`` never
touches ``owner.id`` next to the record's own ``id``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from fauxcall import jsonfmt, jsonpath
from fauxcall.model.fields import (
    CalendarPolicy,
    ComputedPolicy,
    CounterPolicy,
    IgnorePolicy,
    NowPolicy,
    PeriodicPolicy,
    PseudonymPolicy,
    RandomPolicy,
    RelativePolicy,
    SeriesPolicy,
    VolatilePolicy,
)
from fauxcall.timefmt import NS_PER_SECOND, infer_time_format

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from fauxcall.jsonpath import Segment
    from fauxcall.model.fields import FieldPolicy
    from fauxcall.privacy.pii import PiiCategory
    from fauxcall.types import JsonValue

type Translate = Callable[[JsonValue, PiiCategory], JsonValue]
"""Maps a real personal value to the pseudonym the fake knows (``Pseudonymizer.translate``)."""

type Trail = tuple[Segment, ...]

W_STATUS: Final = 5.0
W_CONTENT_TYPE: Final = 1.0
W_HEADER: Final = 0.5
W_SHAPE: Final = 3.0
W_VALUE: Final = 1.0
W_SOFT_VALUE: Final = 0.5
W_LENGTH: Final = 1.0
TIME_TOLERANCE_S: Final = 10.0
"""How far apart (in seconds, relative to each side's response time) time fields may be."""
RELATIVE_TOLERANCE: Final = 0.01
"""``relative`` fields may also drift by this share of their distance to the response time."""
CONTRACT_HEADERS: Final = ("content-type", "location", "retry-after", "link")
SECONDS_PER_DAY: Final = 86_400
_SIMPLE_KEY: Final = re.compile(r"[A-Za-z_][\w-]*")


@dataclass(slots=True)
class Check:
    """One comparison."""

    name: str
    weight: float
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class Observation:
    """One side of a comparison: what came back and when."""

    status: int | None
    headers: Mapping[str, str]
    body: JsonValue
    text: str | None
    at_ns: int
    error: str | None = None


@dataclass(slots=True)
class Comparison:
    """All checks for one exchange pair."""

    label: str
    checks: list[Check] = field(default_factory=list)

    @property
    def total(self) -> float:
        """Total weight."""
        return sum(check.weight for check in self.checks)

    @property
    def passed(self) -> float:
        """Weight of passed checks."""
        return sum(check.weight for check in self.checks if check.passed)

    @property
    def score(self) -> float:
        """Share of passed weight (1.0 when there was nothing to check)."""
        return 1.0 if self.total == 0 else self.passed / self.total

    def add(self, name: str, weight: float, passed: bool, detail: str = "") -> None:  # noqa: FBT001
        """Record a check."""
        self.checks.append(Check(name, weight, passed, detail))

    def failures(self) -> list[Check]:
        """Checks that failed."""
        return [check for check in self.checks if not check.passed]


def type_name(value: JsonValue) -> str:
    """JSON type name of a value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def shape(value: JsonValue) -> JsonValue:
    """A structural fingerprint: keys and leaf types (a list collapses to its first item)."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    name = type_name(value)
    return "number" if name == "integer" else name


def _child(path: str, key: str) -> str:
    if _SIMPLE_KEY.fullmatch(key):
        return f"{path}.{key}"
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'{path}["{escaped}"]'


def shape_differences(real: JsonValue, fake: JsonValue, path: str = "$") -> list[str]:
    """Where the structures differ (missing/extra keys, type changes, null vs a value).

    Integers and other numbers are the same type; lists are compared through their first items.
    """
    if isinstance(real, dict) and isinstance(fake, dict):
        problems = [f"{_child(path, key)}: missing in fake" for key in real if key not in fake]
        problems += [f"{_child(path, key)}: extra in fake" for key in fake if key not in real]
        for key in real:
            if key in fake:
                problems += shape_differences(real[key], fake[key], _child(path, key))
        return problems
    if isinstance(real, list) and isinstance(fake, list):
        if real and fake:
            return shape_differences(real[0], fake[0], f"{path}[0]")
        return []
    if shape(real) != shape(fake):
        return [f"{path}: {type_name(real)} vs {type_name(fake)}"]
    return []


# -- paths and policies ---------------------------------------------------------------------------


def _leaves(value: JsonValue, trail: Trail = ()) -> Iterator[tuple[Trail, JsonValue]]:
    """Every scalar with its concrete path (list indices kept)."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, (*trail, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, (*trail, index))
    else:
        yield trail, value


def _lists(value: JsonValue, trail: Trail = ()) -> Iterator[tuple[Trail, list[JsonValue]]]:
    """Lists that are not inside another list: the body itself, envelope lists like ``$.data``."""
    if isinstance(value, list):
        yield trail, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _lists(item, (*trail, key))


def _generalized(path: Trail) -> Trail:
    return tuple(jsonpath.WILDCARD if isinstance(segment, int) else segment for segment in path)


def _matches(value: JsonValue, path: Trail) -> bool:
    return any(True for _ in jsonpath.iter_matches(value, path))


def _guess_roots(body: JsonValue, relative: Trail) -> list[Trail]:
    """Where records probably sit in ``body`` (see the module docstring)."""
    if isinstance(body, list):
        return [(jsonpath.WILDCARD,)]
    if not isinstance(body, dict) or not relative or _matches(body, relative):
        return [()]
    lists: list[Trail] = [
        (key, jsonpath.WILDCARD)
        for key, value in body.items()
        if isinstance(value, list) and any(isinstance(item, dict) for item in value)
    ]
    if lists:
        return lists
    holders: list[Trail] = [
        (key,)
        for key, value in body.items()
        if isinstance(value, dict) and _matches(value, relative)
    ]
    return holders or [()]


def _policy_index(
    policies: Sequence[FieldPolicy], body: JsonValue, record_roots: Sequence[str] | None
) -> dict[str, FieldPolicy]:
    """Generalized absolute path (``$.data[*].id``) -> the first policy that applies there."""
    roots = None if record_roots is None else [jsonpath.parse(root) for root in record_roots]
    index: dict[str, FieldPolicy] = {}
    for policy in policies:
        try:
            relative = jsonpath.parse(policy.path)
        except ValueError:
            continue
        if policy.scope == "body":
            targets = [relative]
        else:
            bases = roots if roots is not None else _guess_roots(body, relative)
            targets = [(*root, *relative) for root in bases]
        for target in targets:
            index.setdefault(jsonpath.format_path(_generalized(target)), policy)
    return index


# -- values ---------------------------------------------------------------------------------------


def _time_delta(value: JsonValue, at_ns: int) -> tuple[float, bool] | None:
    """Seconds from the response time to a timestamp, and whether it is a time of day.

    Times of day (``"14:05:00"``) are measured from the response's UTC time of day.
    """
    fmt = infer_time_format([value]) if value is not None else None
    if fmt is None:
        return None
    instant = fmt.parse(value)
    if fmt.kind == "time":
        return (instant - at_ns % (SECONDS_PER_DAY * NS_PER_SECOND)) / NS_PER_SECOND, True
    return (instant - at_ns) / NS_PER_SECOND, False


def _time_tolerance(policy: FieldPolicy, delta_s: float) -> float:
    if isinstance(policy, RelativePolicy):
        return max(TIME_TOLERANCE_S, abs(delta_s) * RELATIVE_TOLERANCE)
    if isinstance(policy, PeriodicPolicy):
        return max(TIME_TOLERANCE_S, policy.period_seconds)
    return TIME_TOLERANCE_S


def _compare_time(
    policy: FieldPolicy, real: JsonValue, fake: JsonValue, real_at: int, fake_at: int
) -> tuple[float, bool, str]:
    real_delta, fake_delta = _time_delta(real, real_at), _time_delta(fake, fake_at)
    if real_delta is None or fake_delta is None or real_delta[1] != fake_delta[1]:
        return W_VALUE, real == fake, f"{real!r} vs {fake!r}"
    same_format = infer_time_format([real, fake]) is not None
    difference = abs(real_delta[0] - fake_delta[0])
    if real_delta[1]:
        difference %= SECONDS_PER_DAY
        difference = min(difference, SECONDS_PER_DAY - difference)
    ok = same_format and difference <= _time_tolerance(policy, real_delta[0])
    detail = f"real {real_delta[0]:+.1f}s vs fake {fake_delta[0]:+.1f}s from response time"
    return W_VALUE, ok, detail if same_format else f"{detail}; spelled differently"


def _compare_value(
    real: JsonValue,
    fake: JsonValue,
    policy: FieldPolicy | None,
    real_at: int,
    fake_at: int,
    translate: Translate | None,
) -> tuple[float, bool, str]:
    match policy:
        case IgnorePolicy() | ComputedPolicy():
            return 0.0, True, ""
        case NowPolicy() | CalendarPolicy() | PeriodicPolicy() | RelativePolicy():
            return _compare_time(policy, real, fake, real_at, fake_at)
        case RandomPolicy() | CounterPolicy() | SeriesPolicy() | VolatilePolicy():
            ok = type_name(real) == type_name(fake) or {type_name(real), type_name(fake)} <= {
                "integer",
                "number",
            }
            if ok and isinstance(real, str) and isinstance(fake, str):
                ok = abs(len(real) - len(fake)) <= max(2, len(real) // 4)
            return W_SOFT_VALUE, ok, f"{type_name(real)} vs {type_name(fake)}"
        case PseudonymPolicy():
            if translate is None:
                return (
                    W_SOFT_VALUE,
                    type_name(real) == type_name(fake),
                    "no pseudonym key: type only",
                )
            expected = translate(real, policy.category)
            return W_VALUE, expected == fake, f"pseudonym {expected!r} vs {fake!r}"
        case _:
            return W_VALUE, jsonfmt.plain(real) == jsonfmt.plain(fake), f"{real!r} vs {fake!r}"


def _media_type(headers: Mapping[str, str]) -> str:
    return (headers.get("content-type") or "").split(";")[0].strip().lower()


_ID_NAMES: Final = frozenset({"id", "uuid", "guid"})
_ID_SUFFIXES: Final = ("_id", "-id", "_uuid", "_guid")
_SUCCESS: Final = range(200, 300)


def _id_like(trail: Trail) -> bool:
    """Whether a leaf names an identifier (``id``, ``owner_id``, ``uploadId``, ``uuid``)."""
    name = next((part for part in reversed(trail) if isinstance(part, str)), "")
    lowered = name.lower()
    camel = len(name) > 2 and name.endswith("Id") and name[-3].islower()  # noqa: PLR2004
    return lowered in _ID_NAMES or lowered.endswith(_ID_SUFFIXES) or camel


def _generated(trail: Trail, policy: FieldPolicy | None) -> bool:
    """Whether a value in a write response is one the server makes up (so the sides differ)."""
    return isinstance(policy, RandomPolicy | CounterPolicy) or (policy is None and _id_like(trail))


def _id_text(value: JsonValue) -> str | None:
    """An identifier as text (``None`` for values that can't be identifiers)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, int):
        return str(value)
    return None


def _aliased(value: JsonValue, aliases: Mapping[str, str], pattern: re.Pattern[str]) -> JsonValue:
    """``value`` with the fake's generated ids replaced by the real ones (whole tokens only)."""
    if isinstance(value, dict):
        return {key: _aliased(item, aliases, pattern) for key, item in value.items()}
    if isinstance(value, list):
        return [_aliased(item, aliases, pattern) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        mapped = aliases.get(str(value))
        return int(mapped) if mapped is not None and mapped.lstrip("-").isdigit() else value
    if isinstance(value, str):
        return pattern.sub(lambda match: aliases[match.group(0)], value)
    return value


def translate_ids(fake: Observation, aliases: Mapping[str, str]) -> Observation:
    """The fake's answer as if it had generated the real API's ids (learned earlier in a run)."""
    if not aliases:
        return fake
    keys = sorted(aliases, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w-])(?:" + "|".join(map(re.escape, keys)) + r")(?![\w-])")
    text = pattern.sub(lambda match: aliases[match.group(0)], fake.text) if fake.text else fake.text
    return Observation(
        status=fake.status,
        headers=fake.headers,
        body=_aliased(fake.body, aliases, pattern),
        text=text,
        at_ns=fake.at_ns,
        error=fake.error,
    )


def compare(  # noqa: PLR0913 - every option is a keyword
    label: str,
    real: Observation,
    fake: Observation,
    *,
    policies: Sequence[FieldPolicy] = (),
    translate: Translate | None = None,
    compare_lengths: bool = True,
    max_value_checks: int = 200,
    record_roots: Sequence[str] | None = None,
    aliases: dict[str, str] | None = None,
    created: bool = False,
) -> Comparison:
    """Compare one real and one fake response.

    Args:
        label: What was called (``"GET /pets/1"``); reports map it to an endpoint.
        real: The real API's answer.
        fake: The fake's answer.
        policies: Field policies of the endpoint (and of the scenario).
        translate: Real personal value -> pseudonym, for ``pseudonym`` fields.
        compare_lengths: Whether list lengths must match.
        max_value_checks: At most this many values are compared.
        record_roots: Where records sit in the body (``"$"``, ``"$[*]"``, ``"$.data[*]"``), for
            record-scope policies; guessed when ``None``.
        aliases: Fake id -> real id, learned from earlier write responses of the same scenario:
            the fake's answer is compared as if it had generated the real ids (``No watchlist
            with id 1001`` on the fake matches ``… id 1009`` on the real API). Updated in place.
        created: This call was a write: ids the server made up (id-like fields, ``random`` and
            ``counter`` policies) are compared by type and learned into ``aliases``.
    """
    result = Comparison(label)
    if aliases:
        fake = translate_ids(fake, aliases)
    if real.error or fake.error:
        result.add(
            "transport", W_STATUS, real.error == fake.error, f"real={real.error} fake={fake.error}"
        )
        return result
    result.add("status", W_STATUS, real.status == fake.status, f"{real.status} vs {fake.status}")
    _check_headers(result, real.headers, fake.headers)
    if real.body is None and fake.body is None:
        if real.text is not None or fake.text is not None:
            result.add("text body", W_VALUE, (real.text or "") == (fake.text or ""), "text differs")
        return result
    differences = shape_differences(real.body, fake.body)
    result.add("shape", W_SHAPE, not differences, "; ".join(differences[:5]))
    if compare_lengths:
        _check_lengths(result, real.body, fake.body)
    index = _policy_index(policies, real.body, record_roots)
    learning = created and real.status in _SUCCESS and fake.status in _SUCCESS
    if learning and aliases is not None:
        aliases.update(_generated_ids(real.body, fake.body, index))
    if max_value_checks > 0:
        _check_values(result, real, fake, index, translate, max_value_checks, learning=learning)
    return result


def _generated_ids(
    real: JsonValue, fake: JsonValue, index: Mapping[str, FieldPolicy]
) -> dict[str, str]:
    """Fake id -> real id for every server-made id that differs between two write responses."""
    fake_leaves = dict(_leaves(fake))
    learned: dict[str, str] = {}
    for path, real_value in _leaves(real):
        policy = index.get(jsonpath.format_path(_generalized(path)))
        real_id, fake_id = _id_text(real_value), _id_text(fake_leaves.get(path))
        if _generated(path, policy) and real_id and fake_id and real_id != fake_id:
            learned[fake_id] = real_id
    return learned


def _check_values(
    result: Comparison,
    real: Observation,
    fake: Observation,
    index: Mapping[str, FieldPolicy],
    translate: Translate | None,
    max_value_checks: int,
    *,
    learning: bool,
) -> None:
    """Values field by field, as their policies say (made-up ids of a write: by type)."""
    fake_leaves = dict(_leaves(fake.body))
    checked = 0
    for path, real_value in _leaves(real.body):
        if checked >= max_value_checks:
            break
        if path not in fake_leaves:
            continue
        policy = index.get(jsonpath.format_path(_generalized(path)))
        if learning and policy is None and _generated(path, policy):
            policy = RandomPolicy(path="$", generator="uuid4")  # compared by type
        weight, ok, detail = _compare_value(
            real_value, fake_leaves[path], policy, real.at_ns, fake.at_ns, translate
        )
        if weight:
            result.add(f"value {jsonpath.format_path(path)}", weight, ok, detail)
            checked += 1


def _check_headers(result: Comparison, real: Mapping[str, str], fake: Mapping[str, str]) -> None:
    """Content type (media type only) and the contract headers the real API sent."""
    real_headers = {key.lower(): value for key, value in real.items()}
    fake_headers = {key.lower(): value for key, value in fake.items()}
    real_type, fake_type = _media_type(real_headers), _media_type(fake_headers)
    result.add(
        "content-type", W_CONTENT_TYPE, real_type == fake_type, f"{real_type!r} vs {fake_type!r}"
    )
    for header in CONTRACT_HEADERS[1:]:
        if header in real_headers:
            result.add(f"header {header}", W_HEADER, header in fake_headers, "present in real only")


def _check_lengths(result: Comparison, real: JsonValue, fake: JsonValue) -> None:
    """Lengths of the lists found at the same place on both sides (outside other lists)."""
    fake_lists = dict(_lists(fake))
    for path, items in _lists(real):
        other = fake_lists.get(path)
        if other is not None:
            name = "length" if not path else f"length {jsonpath.format_path(path)}"
            result.add(name, W_LENGTH, len(items) == len(other), f"{len(items)} vs {len(other)}")
