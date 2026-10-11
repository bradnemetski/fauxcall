from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from fauxcall.jsonfmt import JsonFloat
from fauxcall.model.fields import (
    CalendarPolicy,
    ComputedPolicy,
    CounterPolicy,
    EchoPolicy,
    IgnorePolicy,
    NowPolicy,
    PeriodicPolicy,
    PseudonymPolicy,
    RandomPolicy,
    RelativePolicy,
    SeriesPolicy,
    StaticPolicy,
    VolatilePolicy,
)
from fauxcall.testing.compare import (
    TIME_TOLERANCE_S,
    W_CONTENT_TYPE,
    W_HEADER,
    W_LENGTH,
    W_SHAPE,
    W_SOFT_VALUE,
    W_STATUS,
    W_VALUE,
    Check,
    Comparison,
    Observation,
    compare,
    shape,
    shape_differences,
    translate_ids,
    type_name,
)
from fauxcall.timefmt import TimeFormat

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fauxcall.model.fields import FieldPolicy
    from fauxcall.privacy.pii import PiiCategory
    from fauxcall.testing.compare import Translate
    from fauxcall.types import JsonValue

AT = datetime(2026, 10, 11, 1, 17, 26, tzinfo=UTC)
AT_NS = int(AT.timestamp()) * 1_000_000_000
JSON = {"content-type": "application/json"}
ISO = TimeFormat(kind="iso")


def ns(moment: datetime) -> int:
    return int(moment.timestamp()) * 1_000_000_000


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def obs(
    body: JsonValue = None,
    *,
    status: int | None = 200,
    headers: Mapping[str, str] | None = None,
    text: str | None = None,
    at: int = AT_NS,
    error: str | None = None,
) -> Observation:
    return Observation(
        status=status,
        headers=JSON if headers is None else headers,
        body=body,
        text=text,
        at_ns=at,
        error=error,
    )


def checks(comparison: Comparison) -> dict[str, Check]:
    return {check.name: check for check in comparison.checks}


def value_check(
    real: JsonValue,
    fake: JsonValue,
    policy: FieldPolicy | None,
    *,
    real_at: int = AT_NS,
    fake_at: int = AT_NS,
    translate: Translate | None = None,
) -> Check | None:
    """Compare ``{"v": real}`` with ``{"v": fake}`` and return the value check (if any)."""
    result = compare(
        "GET /x",
        obs({"v": real}, at=real_at),
        obs({"v": fake}, at=fake_at),
        policies=[policy] if policy is not None else [],
        translate=translate,
    )
    return checks(result).get("value $.v")


def soft_check(detail: str, *, weight: float = W_SOFT_VALUE) -> Check:
    """The passed check of ``$.v`` (see :func:`value_check`)."""
    return Check(name="value $.v", weight=weight, passed=True, detail=detail)


# -- building blocks -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "name"),
    [
        (None, "null"),
        (True, "boolean"),
        (3, "integer"),
        (3.5, "number"),
        (JsonFloat("1.50"), "number"),
        ("x", "string"),
        ([1], "array"),
        ({"a": 1}, "object"),
    ],
)
def test_type_name(value: JsonValue, name: str) -> None:
    assert type_name(value) == name


def test_shape_collapses_lists_and_merges_number_types() -> None:
    assert shape({"a": [{"b": 1}, {"c": "x"}], "n": 1.5, "i": 2, "e": [], "z": None}) == {
        "a": [{"b": "number"}],
        "n": "number",
        "i": "number",
        "e": [],
        "z": "null",
    }


def test_shape_differences_reports_missing_extra_and_type_changes() -> None:
    real: JsonValue = {"id": 1, "name": "Rex", "tags": [{"t": "a"}], "owner": {"id": 1}}
    fake: JsonValue = {"id": 1.0, "nick": "R", "tags": [{"t": 3}], "owner": {"id": "1"}}
    assert shape_differences(real, fake) == [
        "$.name: missing in fake",
        "$.nick: extra in fake",
        "$.tags[0].t: string vs integer",
        "$.owner.id: integer vs string",
    ]


def test_shape_differences_null_vs_value_and_structures() -> None:
    assert shape_differences({"a": None}, {"a": "x"}) == ["$.a: null vs string"]
    assert shape_differences({"a": 1}, {"a": None}) == ["$.a: integer vs null"]
    assert shape_differences(None, None) == []
    assert shape_differences({"a": {"b": 1}}, {"a": [1]}) == ["$.a: object vs array"]
    assert shape_differences([], [{"a": 1}]) == []
    assert shape_differences([{"a": 1}], []) == []
    assert shape_differences(None, {"a": 1}) == ["$: null vs object"]


def test_shape_differences_quote_odd_keys() -> None:
    assert shape_differences({'unit "price"': 1}, {}) == ['$["unit \\"price\\""]: missing in fake']
    assert shape_differences({"a b": 1}, {"a b": "x"}) == ['$["a b"]: integer vs string']


def test_comparison_scores_by_weight() -> None:
    comparison = Comparison("GET /x")
    assert comparison.score == 1.0
    assert comparison.total == 0
    comparison.add("status", 5.0, passed=True)
    comparison.add("shape", 3.0, passed=False, detail="missing")
    assert comparison.total == 8.0
    assert comparison.passed == 5.0
    assert comparison.score == 5.0 / 8.0
    assert [check.name for check in comparison.failures()] == ["shape"]
    assert comparison.failures()[0].detail == "missing"


# -- status, headers, bodies -----------------------------------------------------------------------


def test_identical_responses_pass_every_check() -> None:
    body: JsonValue = {"id": 1, "tags": ["a", "b"], "price": JsonFloat("1.50")}
    result = compare("GET /pets/1", obs(body), obs({"id": 1, "tags": ["a", "b"], "price": 1.5}))
    assert result.label == "GET /pets/1"
    assert result.score == 1.0
    found = checks(result)
    assert found["status"].weight == W_STATUS
    assert found["content-type"].weight == W_CONTENT_TYPE
    assert found["shape"].weight == W_SHAPE
    assert found["length $.tags"].weight == W_LENGTH
    assert {"value $.id", "value $.tags[0]", "value $.tags[1]", "value $.price"} <= set(found)


def test_status_content_type_and_contract_headers() -> None:
    real = obs(
        {"a": 1},
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Location": "/pets/2",
            "retry-after": "1",
            "x-incidental": "ignored",
        },
    )
    fake = obs({"a": 1}, status=201, headers={"content-type": "application/json", "location": "/"})
    found = checks(compare("POST /pets", real, fake))
    assert not found["status"].passed
    assert found["status"].detail == "200 vs 201"
    assert found["content-type"].passed  # parameters are ignored
    assert found["header location"].passed
    assert found["header location"].weight == W_HEADER
    assert not found["header retry-after"].passed
    assert "header link" not in found
    assert "header x-incidental" not in found


def test_content_type_mismatch() -> None:
    found = checks(compare("GET /x", obs({"a": 1}), obs({"a": 1}, headers={})))
    assert not found["content-type"].passed
    assert found["content-type"].detail == "'application/json' vs ''"


def test_transport_errors_are_compared_alone() -> None:
    same = compare("GET /x", obs(error="ConnectError"), obs(error="ConnectError"))
    assert [(c.name, c.weight, c.passed) for c in same.checks] == [("transport", W_STATUS, True)]
    different = compare("GET /x", obs(error="ReadTimeout"), obs({"a": 1}))
    assert [(c.name, c.passed) for c in different.checks] == [("transport", False)]
    assert different.checks[0].detail == "real=ReadTimeout fake=None"


def test_text_bodies_and_empty_bodies() -> None:
    text = {"content-type": "text/plain"}
    same = checks(compare("GET /t", obs(text="ok", headers=text), obs(text="ok", headers=text)))
    assert same["text body"].passed
    differ = checks(compare("GET /t", obs(text="ok", headers=text), obs(headers=text)))
    assert not differ["text body"].passed
    empty = compare("DELETE /x", obs(status=204, headers={}), obs(status=204, headers={}))
    assert [c.name for c in empty.checks] == ["status", "content-type"]
    assert empty.score == 1.0


def test_json_vs_missing_body_is_a_shape_difference() -> None:
    found = checks(compare("GET /x", obs({"a": 1}), obs(text="<html>")))
    assert not found["shape"].passed
    assert found["shape"].detail == "$: object vs null"


def test_list_lengths_top_level_and_in_envelopes() -> None:
    real = obs({"data": [{"id": 1}, {"id": 2}], "meta": {"tags": ["a"]}})
    fake = obs({"data": [{"id": 1}], "meta": {"tags": ["a"]}})
    found = checks(compare("GET /x", real, fake))
    assert not found["length $.data"].passed
    assert found["length $.data"].detail == "2 vs 1"
    assert found["length $.meta.tags"].passed
    bare = checks(compare("GET /x", obs([1, 2, 3]), obs([1, 2])))
    assert not bare["length"].passed
    nested = checks(compare("GET /x", obs([{"tags": [1, 2]}]), obs([{"tags": [1]}])))
    assert "length $[0].tags" not in nested  # lists inside lists are not counted
    not_a_list = checks(compare("GET /x", obs({"a": [1]}), obs({"a": "x"})))
    assert "length $.a" not in not_a_list  # the shape check reports it
    assert not not_a_list["shape"].passed


def test_lengths_can_be_ignored() -> None:
    result = compare("GET /x", obs([1, 2, 3]), obs([1, 2]), compare_lengths=False)
    assert "length" not in checks(result)
    assert result.score == 1.0  # only the first two values exist on both sides


def test_value_checks_are_limited() -> None:
    body: JsonValue = {"a": 1, "b": 2, "c": 3}
    other: JsonValue = {"a": 9, "b": 9, "c": 9}
    none = compare("GET /x", obs(body), obs(other), max_value_checks=0)
    assert [c.name for c in none.checks] == ["status", "content-type", "shape"]
    two = compare("GET /x", obs(body), obs(other), max_value_checks=2)
    assert [c.name for c in two.checks if c.name.startswith("value")] == ["value $.a", "value $.b"]


def test_values_missing_on_the_fake_side_are_left_to_the_shape_check() -> None:
    found = checks(compare("GET /x", obs({"a": 1, "b": 2}), obs({"a": 1})))
    assert "value $.b" not in found
    assert not found["shape"].passed


def test_scalar_bodies() -> None:
    found = checks(compare("GET /count", obs(3), obs(4)))
    assert not found["value $"].passed
    assert found["value $"].detail == "3 vs 4"


# -- policies --------------------------------------------------------------------------------------


def test_static_and_echo_values_must_match() -> None:
    assert value_check(1, 1, None) == soft_check("1 vs 1", weight=W_VALUE)
    assert value_check(1, JsonFloat("1.0"), StaticPolicy(path="v")) is not None
    mismatch = value_check("EUR", "USD", EchoPolicy(path="v", source="query.base"))
    assert mismatch is not None
    assert not mismatch.passed
    assert mismatch.detail == "'EUR' vs 'USD'"


@pytest.mark.parametrize(
    "policy", [IgnorePolicy(path="v"), ComputedPolicy(path="v", function="handlers.total")]
)
def test_ignored_and_computed_values_are_not_compared(policy: FieldPolicy) -> None:
    assert value_check("a", "b", policy) is None


def test_now_fields_are_compared_relative_to_each_response() -> None:
    policy = NowPolicy(path="v", format=ISO)
    later = AT + timedelta(minutes=5)
    check = value_check(iso(AT), iso(later), policy, fake_at=ns(later))
    assert check is not None
    assert check.passed
    assert check.detail == "real +0.0s vs fake +0.0s from response time"
    stale = value_check(iso(AT), iso(AT), policy, fake_at=ns(later))
    assert stale is not None
    assert not stale.passed
    assert stale.detail == "real +0.0s vs fake -300.0s from response time"


def test_now_fields_tolerate_a_few_seconds() -> None:
    policy = CalendarPolicy(path="v", calendar="nyse", format=ISO)
    close = AT + timedelta(seconds=TIME_TOLERANCE_S - 1)
    far = AT + timedelta(seconds=TIME_TOLERANCE_S + 1)
    near = value_check(iso(AT), iso(close), policy)
    assert near is not None
    assert near.passed
    beyond = value_check(iso(AT), iso(far), policy)
    assert beyond is not None
    assert not beyond.passed


def test_time_fields_must_be_spelled_the_same_way() -> None:
    policy = NowPolicy(path="v", format=ISO)
    millis = AT.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    check = value_check(iso(AT), millis, policy)
    assert check is not None
    assert not check.passed
    assert check.detail.endswith("; spelled differently")
    epoch = value_check(int(AT.timestamp()), int(AT.timestamp()) + 2, policy)
    assert epoch is not None
    assert epoch.passed


def test_unparseable_time_values_fall_back_to_equality() -> None:
    policy = NowPolicy(path="v", format=ISO)
    same = value_check("soon", "soon", policy)
    assert same is not None
    assert same.passed
    different = value_check("soon", iso(AT), policy)
    assert different is not None
    assert not different.passed
    assert different.detail == f"'soon' vs '{iso(AT)}'"
    nulls = value_check(None, None, policy)
    assert nulls is not None
    assert nulls.passed


def test_time_of_day_fields_wrap_around_midnight() -> None:
    policy = NowPolicy(path="v", format=TimeFormat(kind="time"))
    before = datetime(2026, 10, 10, 23, 59, 58, tzinfo=UTC)
    after = datetime(2026, 10, 11, 0, 0, 1, tzinfo=UTC)
    exact = value_check("23:59:58", "00:00:01", policy, real_at=ns(before), fake_at=ns(after))
    assert exact is not None
    assert exact.passed
    # the fake's value lags 2s behind its response, across midnight: still close enough
    lagging = value_check("23:59:58", "23:59:59", policy, real_at=ns(before), fake_at=ns(after))
    assert lagging is not None
    assert lagging.passed
    off = value_check("12:00:00", "18:00:00", policy, real_at=ns(AT), fake_at=ns(AT))
    assert off is not None
    assert not off.passed
    mixed = value_check("12:00:00", iso(AT), policy)
    assert mixed is not None
    assert not mixed.passed


def test_relative_fields_tolerate_one_percent_of_their_distance() -> None:
    policy = RelativePolicy(path="v", format=ISO)
    in_a_month = AT + timedelta(days=30)
    drifted = in_a_month + timedelta(hours=2)  # 2h of 30 days is < 1%
    check = value_check(iso(in_a_month), iso(drifted), policy)
    assert check is not None
    assert check.passed
    too_far = value_check(iso(in_a_month), iso(in_a_month + timedelta(days=2)), policy)
    assert too_far is not None
    assert not too_far.passed


def test_periodic_fields_tolerate_one_period() -> None:
    policy = PeriodicPolicy(path="v", period_seconds=300, format=ISO)
    real_now = datetime(2026, 10, 11, 12, 4, 59, tzinfo=UTC)
    fake_now = datetime(2026, 10, 11, 12, 5, 1, tzinfo=UTC)
    check = value_check(
        "2026-10-11T12:00:00Z",
        "2026-10-11T12:05:00Z",
        policy,
        real_at=ns(real_now),
        fake_at=ns(fake_now),
    )
    assert check is not None
    assert check.passed
    late = value_check("2026-10-11T12:00:00Z", "2026-10-11T12:20:00Z", policy)
    assert late is not None
    assert not late.passed


@pytest.mark.parametrize(
    "policy",
    [
        RandomPolicy(path="v", generator="uuid4"),
        CounterPolicy(path="v"),
        SeriesPolicy(path="v", series="prices"),
        VolatilePolicy(path="v"),
    ],
)
def test_changing_values_are_compared_by_type(policy: FieldPolicy) -> None:
    same_type = value_check("a1b2c3d4", "e5f6a7b8", policy)
    assert same_type == soft_check("string vs string")
    numbers = value_check(1, JsonFloat("2.5"), policy)
    assert numbers is not None
    assert numbers.passed
    other_type = value_check(1, "1", policy)
    assert other_type is not None
    assert not other_type.passed


def test_changing_strings_must_have_a_similar_length() -> None:
    policy = RandomPolicy(path="v", generator="hex")
    assert value_check("abcd", "abcdef", policy) == soft_check("string vs string")
    long = value_check("a" * 40, "a" * 52, policy)
    assert long is not None
    assert not long.passed


def test_pseudonyms_are_compared_through_the_key() -> None:
    def translate(value: JsonValue, category: PiiCategory) -> JsonValue:
        assert category == "email"
        return f"user.{str(value).split('@')[0]}@example.com"

    policy = PseudonymPolicy(path="v", category="email")
    good = value_check("jane@corp.com", "user.jane@example.com", policy, translate=translate)
    assert good is not None
    assert good.passed
    assert good.weight == W_VALUE
    bad = value_check("jane@corp.com", "jane@corp.com", policy, translate=translate)
    assert bad is not None
    assert not bad.passed
    assert bad.detail == "pseudonym 'user.jane@example.com' vs 'jane@corp.com'"
    no_key = value_check("jane@corp.com", "someone@else.org", policy)
    assert no_key == soft_check("no pseudonym key: type only")


# -- where record policies apply -------------------------------------------------------------------


def _policy_hits(
    real: JsonValue,
    fake: JsonValue,
    policies: Sequence[FieldPolicy],
    record_roots: Sequence[str] | None = None,
) -> dict[str, bool]:
    result = compare("GET /x", obs(real), obs(fake), policies=policies, record_roots=record_roots)
    return {c.name: c.passed for c in result.checks if c.name.startswith("value")}


def test_record_policies_apply_to_the_record_not_to_nested_namesakes() -> None:
    policies = [IgnorePolicy(path="$.id")]
    hits = _policy_hits({"id": 1, "owner": {"id": 7}}, {"id": 2, "owner": {"id": 8}}, policies)
    assert "value $.id" not in hits  # ignored
    assert hits["value $.owner.id"] is False  # not the record's id: compared as a static value


def test_record_policies_find_records_in_bare_lists() -> None:
    hits = _policy_hits(
        [{"id": 1, "n": "a"}, {"id": 2, "n": "b"}],
        [{"id": 5, "n": "a"}, {"id": 6, "n": "b"}],
        [IgnorePolicy(path="id")],
    )
    assert hits == {"value $[0].n": True, "value $[1].n": True}


def test_record_policies_find_records_in_envelopes() -> None:
    real: JsonValue = {"data": [{"id": 1}], "meta": {"id": "req-1"}, "total": 1}
    fake: JsonValue = {"data": [{"id": 9}], "meta": {"id": "req-2"}, "total": 1}
    hits = _policy_hits(real, fake, [IgnorePolicy(path="id")])
    assert "value $.data[0].id" not in hits
    assert hits["value $.meta.id"] is False
    single: JsonValue = {"result": {"id": 1, "seen": 2}, "ok": True}
    other: JsonValue = {"result": {"id": 3, "seen": 2}, "ok": True}
    assert _policy_hits(single, other, [IgnorePolicy(path="id")]) == {
        "value $.result.seen": True,
        "value $.ok": True,
    }


def test_unguessable_records_fall_back_to_the_body() -> None:
    hits = _policy_hits({"a": {"b": 1}}, {"a": {"b": 2}}, [IgnorePolicy(path="id")])
    assert hits == {"value $.a.b": False}
    scalar = compare("GET /x", obs(3), obs(4), policies=[IgnorePolicy(path="id")])
    assert "value $" in checks(scalar)


def test_explicit_record_roots_override_the_guess() -> None:
    real: JsonValue = {"id": "page-1", "items": [{"id": 1}], "extra": [{"id": 2}]}
    fake: JsonValue = {"id": "page-2", "items": [{"id": 3}], "extra": [{"id": 4}]}
    hits = _policy_hits(real, fake, [IgnorePolicy(path="id")], record_roots=["$.items[*]"])
    assert hits == {"value $.id": False, "value $.extra[0].id": False}


def test_root_policies_and_body_scope_policies() -> None:
    root = compare("GET /x", obs("abc"), obs("xyz"), policies=[IgnorePolicy(path="$")])
    assert "value $" not in checks(root)
    body_policy = IgnorePolicy(path="$.meta.request_id", scope="body")
    hits = _policy_hits(
        {"meta": {"request_id": "a"}, "id": 1},
        {"meta": {"request_id": "b"}, "id": 1},
        [body_policy],
    )
    assert hits == {"value $.id": True}
    nested = _policy_hits(
        {"data": {"meta": {"request_id": "a"}}},
        {"data": {"meta": {"request_id": "b"}}},
        [body_policy],
    )
    assert nested == {"value $.data.meta.request_id": False}  # body paths are exact


def test_list_index_policies_cover_every_element_and_first_policy_wins() -> None:
    policies: list[FieldPolicy] = [
        IgnorePolicy(path="$.items[0].v", scope="body"),
        VolatilePolicy(path="$.items[*].v", scope="body"),
    ]
    hits = _policy_hits({"items": [{"v": 1}, {"v": 2}]}, {"items": [{"v": 3}, {"v": 4}]}, policies)
    assert hits == {}


def test_invalid_policy_paths_are_skipped() -> None:
    hits = _policy_hits({"a": 1}, {"a": 2}, [IgnorePolicy(path="$..a"), IgnorePolicy(path="b")])
    assert hits == {"value $.a": False}


def test_translating_ids_replaces_whole_tokens_only() -> None:
    aliases = {"1001": "1009", "4048b3e9-aaaa": "02c9e9fa-bbbb"}
    fake = obs(
        {
            "id": 1001,
            "big": 21001,
            "ref": "4048b3e9-aaaa",
            "msg": "No upload with id '4048b3e9-aaaa'; see 1001 not 11001 or 1001x.",
            "flag": True,
            "none": None,
            "ratio": 1001.5,
            "tags": [1001, "1001"],
        },
        text="id 1001",
    )
    translated = translate_ids(fake, aliases)
    assert translated.body == {
        "id": 1009,
        "big": 21001,
        "ref": "02c9e9fa-bbbb",
        "msg": "No upload with id '02c9e9fa-bbbb'; see 1009 not 11001 or 1001x.",
        "flag": True,
        "none": None,
        "ratio": 1001.5,
        "tags": [1009, "1009"],
    }
    assert translated.text == "id 1009"
    assert translate_ids(fake, {}) is fake
    word = translate_ids(obs({"id": "abc"}), {"abc": "xyz-not-a-number"})
    assert word.body == {"id": "xyz-not-a-number"}
    odd = translate_ids(obs({"n": 5}), {"5": "five"})  # a non-numeric counterpart: left alone
    assert odd.body == {"n": 5}


def test_writes_learn_generated_ids_and_compare_them_by_type() -> None:
    aliases: dict[str, str] = {}
    real = obs(
        {"id": "r-1", "owner_id": 7, "uploadId": "u-1", "count": 3, "seq": 10, "rank_id": 1.5},
        status=201,
    )
    fake = obs(
        {"id": "f-1", "owner_id": 8, "uploadId": "u-2", "count": 3, "seq": 99, "rank_id": 2.5},
        status=201,
    )
    result = compare(
        "POST /x",
        real,
        fake,
        policies=[CounterPolicy(path="$.seq", scope="body")],
        aliases=aliases,
        created=True,
    )
    assert result.failures() == []
    assert aliases == {"f-1": "r-1", "8": "7", "u-2": "u-1", "99": "10"}
    later = compare("GET /x", obs({"owner_id": 7}), obs({"owner_id": 8}), aliases=aliases)
    assert later.failures() == []
    unlearned = compare("PUT /x", obs({"id": 1}), obs({"id": 2}), aliases={}, created=False)
    assert [c.name for c in unlearned.failures()] == ["value $.id"]


def test_streams_are_compared_by_structure_not_values() -> None:
    real = Observation(
        200, {}, [{"trade": {"price": "1.0", "time": "a"}}, {"heartbeat": ""}], None, 0, stream=True
    )
    fake = Observation(
        200, {}, [{"trade": {"price": "9.9", "time": "b"}}, {"heartbeat": ""}], None, 0, stream=True
    )
    assert compare("GET /stream", real, fake).failures() == []
    renamed = Observation(
        200, {}, [{"quote": {"price": "1.0", "time": "a"}}, {"heartbeat": ""}], None, 0, stream=True
    )
    assert [c.name for c in compare("GET /stream", real, renamed).failures()] == ["shape"]
    shorter = Observation(200, {}, [{"trade": {"price": "1.0", "time": "a"}}], None, 0, stream=True)
    assert [c.name for c in compare("GET /stream", real, shorter).failures()] == ["length"]
