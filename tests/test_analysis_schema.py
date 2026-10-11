from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from fauxcall.analysis.schema import (
    SchemaDifference,
    compare_schemas,
    has_string_format,
    infer_schema,
    json_type,
    merge_schemas,
    string_format,
)
from fauxcall.jsonfmt import JsonFloat, loads
from fauxcall.timefmt import TimeFormat

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fauxcall.types import JsonValue

COINBASE = (
    '{"ask":"82940.02","bid":"82940.01","volume":"1509.40500051","trade_id":1104620943,'
    '"price":"82940.02","size":"0.000012","time":"2026-10-11T01:17:28.087060570Z",'
    '"rfq_volume":"28.844121"}'
)
TIMEAPI = (
    '{"date_time":"2026-10-10T21:17:26.379953-04:00","date":"2026-10-10","time":"21:17:26.379953",'
    '"day_of_week":"Saturday","dst_active":true,"timezone":"America/New_York",'
    '"utc_offset_seconds":-14400}'
)
PETS = (
    '[{"id":1,"name":"Pet1","photoUrls":["a"],"tags":[],"status":"available",'
    '"category":{"id":1,"name":"Dogs"}},'
    '{"id":2,"name":"Pet2","photoUrls":[],"tags":[{"id":5,"name":"friendly"}],"status":"sold"},'
    '{"id":3,"photoUrls":["https://img.example.com/3.png"],"status":"available","category":null}]'
)
ACTIVITIES = (
    '[{"id":1,"title":"Activity 1","dueDate":"2026-10-11T02:17:26.8440216Z","completed":false},'
    '{"id":2,"title":"Activity 2","dueDate":"2026-10-11T03:17:26.8440245Z","completed":true}]'
)
FRANKFURTER = (
    '{"amount":1.0,"base":"EUR","date":"2026-10-09",'
    '"rates":{"AUD":1.6889,"GBP":0.84023,"JPY":169.53,"USD":1.1206}}'
)


def _assert_validates(schema: Mapping[str, JsonValue], values: list[JsonValue]) -> None:
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for value in values:
        errors = [error.message for error in validator.iter_errors(value)]
        assert not errors, (value, errors)


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _props(schema: JsonValue) -> dict[str, JsonValue]:
    return _obj(_obj(schema)["properties"])


# ---------------------------------------------------------------------------
# classification helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "null"),
        (True, "boolean"),
        (0, "integer"),
        (1.5, "number"),
        (JsonFloat("1.50"), "number"),
        ("x", "string"),
        ({}, "object"),
        ([], "array"),
    ],
)
def test_json_type(value: JsonValue, expected: str) -> None:
    assert json_type(value) == expected


def test_json_type_rejects_non_json() -> None:
    with pytest.raises(TypeError, match="tuple is not a JSON value"):
        json_type((1, 2))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3fa85f64-5717-4562-b3fc-2c963f66afa6", "uuid"),
        ("3FA85F64-5717-4562-B3FC-2C963F66AFA6", "uuid"),
        ("jane.doe+tag@mail.example.com", "email"),
        ("https://api.frankfurter.app/latest?from=EUR", "uri"),
        ("http://localhost:8080", "uri"),
        ("192.168.0.1", "ipv4"),
        ("2001:db8::1", "ipv6"),
        ("::1", "ipv6"),
        ("999.1.1.1", None),  # looks like IPv4, is not
        ("21:17", None),  # looks like IPv6 characters, is not
        ("fe80::1%eth0", None),
        ("ftp://example.com/file", None),  # only http(s) URLs count as uri
        ("user@localhost", None),
        ("not-a-uuid-3fa85f64", None),
        ("", None),
    ],
)
def test_string_format(text: str, expected: str | None) -> None:
    assert string_format(text) == expected
    if expected is not None:
        assert has_string_format(text, expected)


# ---------------------------------------------------------------------------
# infer_schema - types and unions
# ---------------------------------------------------------------------------


def test_scalar_types_and_examples() -> None:
    assert infer_schema([None]) == {"type": "null"}
    assert infer_schema([True, False, True]) == {"type": "boolean", "examples": [True, False]}
    assert infer_schema([1, 2]) == {"type": "integer", "examples": [1, 2]}
    assert infer_schema([1.5]) == {"type": "number", "examples": [1.5]}
    assert infer_schema(["a"]) == {"type": "string", "examples": ["a"]}
    assert infer_schema([]) == {}


def test_integers_and_floats_fold_into_number_at_the_first_position() -> None:
    schema = infer_schema(["x", 1, 2.5, True, 1.0])
    assert schema == {
        "type": ["string", "number", "boolean"],
        "examples": ["x", 1, 2.5],
    }
    # 1 and 1.0 are the same JSON value: one example, the first spelling.
    assert infer_schema([1, 1.0], max_examples=5)["examples"] == [1]
    # True is not 1.
    assert infer_schema([1, True], max_examples=5)["examples"] == [1, True]


def test_scalar_unions_use_type_lists_with_null_last() -> None:
    schema = infer_schema([None, "a", 3, None])
    assert schema == {"type": ["string", "integer", "null"], "examples": ["a", 3]}
    _assert_validates(schema, [None, "a", 3])


def test_nullable_object_keeps_a_type_list() -> None:
    schema = infer_schema([{"a": 1}, None])
    assert schema == {
        "type": ["object", "null"],
        "properties": {"a": {"type": "integer", "examples": [1]}},
        "required": ["a"],
    }


def test_unions_with_containers_use_any_of() -> None:
    values: list[JsonValue] = [{"a": 1}, [1, "x"], "s", None, 2]
    schema = infer_schema(values)
    assert schema == {
        "anyOf": [
            {
                "type": "object",
                "properties": {"a": {"type": "integer", "examples": [1]}},
                "required": ["a"],
            },
            {"type": "array", "items": {"type": ["integer", "string"], "examples": [1, "x"]}},
            {"type": "string", "examples": ["s"]},
            {"type": "integer", "examples": [2]},
            {"type": "null"},
        ]
    }
    _assert_validates(schema, values)
    assert infer_schema([[], {}]) == {"anyOf": [{"type": "array"}, {"type": "object"}]}


def test_non_json_values_are_rejected() -> None:
    with pytest.raises(TypeError, match="set is not a JSON value"):
        infer_schema([{"a": {1, 2}}])  # type: ignore[dict-item]


@pytest.mark.parametrize("kwargs", [{"enum_threshold": -1}, {"max_examples": -1}])
def test_negative_limits_are_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        infer_schema([1], **kwargs)


# ---------------------------------------------------------------------------
# infer_schema - objects and arrays
# ---------------------------------------------------------------------------


def test_properties_and_required_keep_first_seen_order() -> None:
    schema = infer_schema([{"b": 1, "a": 2}, {"c": 3, "a": 4, "b": 5}])
    assert list(_props(schema)) == ["b", "a", "c"]
    assert schema["required"] == ["b", "a"]
    assert infer_schema([{}, {}]) == {"type": "object"}
    assert infer_schema([{"a": 1}, {}]) == {
        "type": "object",
        "properties": {"a": {"type": "integer", "examples": [1]}},
    }


def test_array_items_merge_every_element_of_every_sample() -> None:
    schema = infer_schema([[1, 2], [], [3.5, None]])
    assert schema == {
        "type": "array",
        "items": {"type": ["number", "null"], "examples": [1, 2, 3.5]},
    }
    assert infer_schema([[], []]) == {"type": "array"}


def test_nested_arrays_of_objects() -> None:
    values: list[JsonValue] = [
        {"matrix": [[1, 2], [3]], "rows": [{"cells": [{"v": "a"}]}, {"cells": []}]},
        {"matrix": [], "rows": [{"cells": [{"v": "b", "w": None}]}]},
    ]
    schema = infer_schema(values)
    matrix = _obj(_props(schema)["matrix"])
    rows = _obj(matrix["items"])
    assert matrix["type"] == rows["type"] == "array"
    assert rows["items"] == {"type": "integer", "examples": [1, 2, 3]}
    cells = _obj(_obj(_props(_obj(_props(schema)["rows"])["items"])["cells"])["items"])
    assert cells == {
        "type": "object",
        "properties": {
            "v": {"type": "string", "examples": ["a", "b"]},
            "w": {"type": "null"},
        },
        "required": ["v"],
    }
    _assert_validates(schema, values)


# ---------------------------------------------------------------------------
# infer_schema - formats and timestamps
# ---------------------------------------------------------------------------


def _time_format(schema: JsonValue) -> TimeFormat:
    return TimeFormat.model_validate(_obj(schema)["x-time-format"])


def test_timestamp_formats_record_the_exact_spelling() -> None:
    coinbase = infer_schema([loads(COINBASE)])
    time = _props(coinbase)["time"]
    assert _obj(time)["format"] == "date-time"
    assert _obj(time)["x-time-format"] == {"kind": "iso", "fraction_digits": 9}
    assert _time_format(time) == TimeFormat(kind="iso", fraction_digits=9, tz_style="Z")

    timeapi = _props(infer_schema([loads(TIMEAPI)]))
    assert _obj(timeapi["date_time"])["format"] == "date-time"
    assert _time_format(timeapi["date_time"]) == TimeFormat(
        kind="iso", fraction_digits=6, tz_style="+HH:MM"
    )
    assert _obj(timeapi["date"])["format"] == "date"
    assert _time_format(timeapi["date"]) == TimeFormat(kind="date")
    assert _obj(timeapi["time"])["format"] == "time"
    assert _time_format(timeapi["time"]) == TimeFormat(kind="time", fraction_digits=6)
    for plain in ("day_of_week", "timezone"):
        assert "format" not in _obj(timeapi[plain])
        assert "x-time-format" not in _obj(timeapi[plain])

    due = _obj(_props(_obj(infer_schema([loads(ACTIVITIES)])["items"]))["dueDate"])
    assert due["format"] == "date-time"
    assert _time_format(due) == TimeFormat(kind="iso", fraction_digits=7)


def test_time_format_must_fit_every_string() -> None:
    schema = infer_schema(["2026-10-10", "2026-10-11T01:17:28Z", None])
    assert schema == {
        "type": ["string", "null"],
        "examples": ["2026-10-10", "2026-10-11T01:17:28Z"],
    }
    # Non-timestamp strings disqualify the field, also when they come first.
    assert "format" not in infer_schema(["Saturday", "2026-10-10"])
    assert "format" not in infer_schema(["2026-10-10", "x" * 80])


def test_non_standard_time_spellings_only_get_x_time_format() -> None:
    http_dates = infer_schema(["Sun, 11 Oct 2026 01:17:28 GMT"])
    assert "format" not in http_dates
    assert http_dates["x-time-format"] == {"kind": "http_date"}
    us_dates = infer_schema(["10/11/2026 01:17:28"])
    assert us_dates["x-time-format"] == {"kind": "strftime", "pattern": "%m/%d/%Y %H:%M:%S"}


def test_numeric_strings_are_not_timestamps() -> None:
    # Epoch guesses on strings are left to the field profile; prices are never timestamps.
    for values in (["82940.02", "82940.01"], ["1791681447"]):
        schema = infer_schema(values)
        assert "format" not in schema
        assert "x-time-format" not in schema


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["3fa85f64-5717-4562-b3fc-2c963f66afa6", "7c9e6679-7425-40de-944b-e07fc1f90ae7"], "uuid"),
        (["a@example.com", "b@example.org"], "email"),
        (["https://a.example.com/x", "http://b.example.com"], "uri"),
        (["10.0.0.1", "8.8.8.8"], "ipv4"),
        (["2001:db8::1", "::1"], "ipv6"),
    ],
)
def test_simple_formats_shared_by_every_string(values: list[JsonValue], expected: str) -> None:
    schema = infer_schema([*values, None, 7])
    assert schema["format"] == expected
    assert "x-time-format" not in schema
    _assert_validates(schema, [*values, None, 7])
    assert "format" not in infer_schema([*values, "plain text"])
    assert "format" not in infer_schema(["plain text", *values])


# ---------------------------------------------------------------------------
# infer_schema - examples and enums
# ---------------------------------------------------------------------------


def test_examples_are_distinct_first_seen_and_capped() -> None:
    assert infer_schema(["c", "a", "c", "b", "d"])["examples"] == ["c", "a", "b"]
    assert infer_schema(["c", "a", "b"], max_examples=1)["examples"] == ["c"]
    assert "examples" not in infer_schema(["c"], max_examples=0)


def test_examples_keep_float_spellings() -> None:
    value = loads('{"price":105.50,"qty":1.0}')
    schema = infer_schema([value])
    for name, lexeme in (("price", "105.50"), ("qty", "1.0")):
        examples = _obj(_props(schema)[name])["examples"]
        assert isinstance(examples, list)
        (example,) = examples
        assert isinstance(example, JsonFloat)
        assert example.lexeme == lexeme


def test_enum_requires_few_distinct_values_seen_often() -> None:
    statuses: list[JsonValue] = ["available", "sold", "available", "pending", "sold", "sold"]
    schema = infer_schema(statuses, enum_threshold=3)
    assert schema["enum"] == ["available", "sold", "pending"]
    _assert_validates(schema, statuses)
    assert "enum" not in infer_schema(statuses)  # threshold 0: never
    assert "enum" not in infer_schema(statuses, enum_threshold=2)  # too many distinct values
    assert "enum" not in infer_schema(statuses[:3], enum_threshold=3)  # 3 samples < 2 x 2


def test_enum_includes_null_and_mixed_scalars() -> None:
    values: list[JsonValue] = [1, "a", None, 1, "a", 1.0, None]
    schema = infer_schema(values, enum_threshold=2)
    assert schema == {
        "type": ["number", "string", "null"],  # 1.0 is a float
        "enum": [1, "a", None],
        "examples": [1, "a"],
    }
    _assert_validates(schema, values)
    folded = infer_schema([1, 2.5, 1, 2.5], enum_threshold=2)
    assert folded == {"type": "number", "enum": [1, 2.5], "examples": [1, 2.5]}


def test_enum_per_any_of_branch() -> None:
    values: list[JsonValue] = ["on", "off", "on", "off", {"mode": "auto"}]
    schema = infer_schema(values, enum_threshold=2)
    branches = schema["anyOf"]
    assert isinstance(branches, list)
    assert branches[0] == {"type": "string", "enum": ["on", "off"], "examples": ["on", "off"]}
    _assert_validates(schema, values)


# ---------------------------------------------------------------------------
# infer_schema - realistic payloads, scale and depth
# ---------------------------------------------------------------------------


def test_coinbase_ticker() -> None:
    ticker = loads(COINBASE)
    schema = infer_schema([ticker])
    assert schema["required"] == list(_obj(ticker))
    props = _props(schema)
    assert props["trade_id"] == {"type": "integer", "examples": [1104620943]}
    assert props["ask"] == {"type": "string", "examples": ["82940.02"]}
    _assert_validates(schema, [ticker])


def test_petstore_pets_with_missing_fields() -> None:
    pets = loads(PETS)
    assert isinstance(pets, list)
    schema = infer_schema(pets, enum_threshold=3)
    assert schema["required"] == ["id", "photoUrls", "status"]
    props = _props(schema)
    assert props["name"] == {"type": "string", "examples": ["Pet1", "Pet2"]}
    assert props["tags"] == {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "examples": [5]},
                "name": {"type": "string", "examples": ["friendly"]},
            },
            "required": ["id", "name"],
        },
    }
    assert _obj(props["category"])["type"] == ["object", "null"]
    assert props["photoUrls"] == {
        "type": "array",
        "items": {"type": "string", "examples": ["a", "https://img.example.com/3.png"]},
    }
    assert props["status"] == {"type": "string", "examples": ["available", "sold"]}
    _assert_validates(schema, pets)
    # The list endpoint returns them as one array body.
    _assert_validates(infer_schema([pets]), [pets])


def test_frankfurter_rates() -> None:
    body = loads(FRANKFURTER)
    schema = infer_schema([body])
    props = _props(schema)
    assert props["amount"] == {"type": "number", "examples": [1.0]}
    assert _obj(props["date"])["format"] == "date"
    assert list(_props(props["rates"])) == ["AUD", "GBP", "JPY", "USD"]
    _assert_validates(schema, [body])


def test_large_inputs_are_handled_in_one_pass() -> None:
    records: list[JsonValue] = [
        {
            "id": index,
            "price": JsonFloat(f"{index}.50"),
            "updated": f"2026-10-11T01:{index // 60 % 60:02d}:{index % 60:02d}.{index:06d}Z",
            "tags": ["a", "b"] if index % 2 else [],
            "note": None if index % 3 else "x",
        }
        for index in range(20_000)
    ]
    schema = infer_schema(iter(records))
    props = _props(_obj(schema))
    assert schema["required"] == ["id", "price", "updated", "tags", "note"]
    assert props["id"] == {"type": "integer", "examples": [0, 1, 2]}
    assert _obj(props["updated"])["x-time-format"] == {"kind": "iso", "fraction_digits": 6}
    assert props["note"] == {"type": ["string", "null"], "examples": ["x"]}


def test_deep_nesting_does_not_recurse() -> None:
    depth = 5_000
    value: JsonValue = "leaf"
    for level in range(depth):
        value = [value] if level % 2 else {"k": value}
    schema: JsonValue = infer_schema([value])
    for level in reversed(range(depth)):
        node = _obj(schema)
        if level % 2:
            assert node["type"] == "array"
            schema = node["items"]
        else:
            assert node["type"] == "object"
            assert node["required"] == ["k"]
            schema = _props(node)["k"]
    assert schema == {"type": "string", "examples": ["leaf"]}
    merged = merge_schemas(infer_schema([value]), infer_schema([value]))
    assert merged == infer_schema([value, value])


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

_SCALARS = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**12), max_value=10**12)
    | st.floats(allow_nan=False, allow_infinity=False, width=32)
    | st.text(alphabet="abcxyz", max_size=4)
)
_JSON = st.recursive(
    _SCALARS,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.sampled_from(["a", "b", "c", "id"]), children, max_size=4)
    ),
    max_leaves=20,
)


@settings(max_examples=150, deadline=None)
@given(st.lists(_JSON, max_size=6), st.integers(min_value=0, max_value=4))
def test_inferred_schema_validates_every_input(values: list[JsonValue], threshold: int) -> None:
    schema = infer_schema(values, enum_threshold=threshold)
    _assert_validates(schema, values)


@settings(max_examples=150, deadline=None)
@given(st.lists(_JSON, max_size=5), st.lists(_JSON, max_size=5))
def test_merge_equals_inference_over_both_sample_sets(
    first: list[JsonValue], second: list[JsonValue]
) -> None:
    merged = merge_schemas(infer_schema(first), infer_schema(second))
    # Examples a side capped across a type list cannot come back when the merge needs anyOf.
    assert _without_examples(merged) == _without_examples(infer_schema([*first, *second]))
    _assert_validates(merged, [*first, *second])


def _without_examples(schema: JsonValue) -> JsonValue:
    if isinstance(schema, list):
        return [_without_examples(item) for item in schema]
    if isinstance(schema, dict):
        return {k: _without_examples(v) for k, v in schema.items() if k != "examples"}
    return schema


def test_merge_cannot_recover_examples_capped_across_a_type_list() -> None:
    first: list[JsonValue] = [[False, True, 0, 1]]
    second: list[JsonValue] = [[[]]]
    assert infer_schema(first) == {
        "type": "array",
        "items": {"type": ["boolean", "integer"], "examples": [False, True, 0]},
    }
    merged = _obj(merge_schemas(infer_schema(first), infer_schema(second))["items"])
    direct = _obj(infer_schema([*first, *second])["items"])
    assert merged["anyOf"] == [
        {"type": "boolean", "examples": [False, True]},
        {"type": "integer", "examples": [0]},
        {"type": "array"},
    ]
    assert _obj(direct["anyOf"][1])["examples"] == [0, 1]  # type: ignore[index]


# ---------------------------------------------------------------------------
# merge_schemas
# ---------------------------------------------------------------------------

UUID_A = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
UUID_B = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def test_merge_unites_properties_and_intersects_required() -> None:
    a = infer_schema([{"a": 1, "b": "x"}])
    b = infer_schema([{"b": "y", "c": None}])
    assert merge_schemas(a, b) == {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "examples": [1]},
            "b": {"type": "string", "examples": ["x", "y"]},
            "c": {"type": "null"},
        },
        "required": ["b"],
    }


def test_merge_keeps_formats_only_where_both_sides_agree() -> None:
    assert merge_schemas(infer_schema([UUID_A]), infer_schema([UUID_B]))["format"] == "uuid"
    assert "format" not in merge_schemas(infer_schema([UUID_A]), infer_schema(["a@b.example"]))
    assert "format" not in merge_schemas(infer_schema([UUID_A]), infer_schema(["plain"]))
    # A side without strings does not veto the other side's format.
    merged = merge_schemas(infer_schema([1]), infer_schema([UUID_A]))
    assert merged == {"type": ["integer", "string"], "format": "uuid", "examples": [1, UUID_A]}


def test_merge_time_formats() -> None:
    millis = infer_schema(["2026-10-11T01:17:28.123Z"])
    micros = infer_schema(["2026-10-11T01:17:28.123456Z"])
    same = merge_schemas(millis, infer_schema(["2026-10-12T00:00:00.500Z"]))
    assert same["format"] == "date-time"
    assert same["x-time-format"] == {"kind": "iso", "fraction_digits": 3}
    different = merge_schemas(millis, micros)
    assert different["format"] == "date-time"
    assert "x-time-format" not in different
    http = infer_schema(["Sun, 11 Oct 2026 01:17:28 GMT"])
    assert merge_schemas(http, http)["x-time-format"] == {"kind": "http_date"}


def test_merge_enums() -> None:
    xs = infer_schema(["x", "x"], enum_threshold=2)
    ys = infer_schema(["y", "y"], enum_threshold=2)
    assert xs["enum"] == ["x"]
    assert merge_schemas(xs, ys)["enum"] == ["x", "y"]
    assert "enum" not in merge_schemas(xs, infer_schema(["z"]))
    nullable = merge_schemas(xs, {"type": "null"})
    assert nullable == {"type": ["string", "null"], "enum": ["x", None], "examples": ["x"]}
    numbers = merge_schemas(
        {"type": "integer", "enum": [1]}, {"type": "number", "enum": [2.5, "x"]}
    )
    assert numbers == {"type": "number", "enum": [1, 2.5]}


def test_merge_examples_are_capped() -> None:
    a = infer_schema([1, 2, 3])
    b = infer_schema([3, 4, 5])
    assert merge_schemas(a, b)["examples"] == [1, 2, 3]
    assert merge_schemas(a, b, max_examples=5)["examples"] == [1, 2, 3, 4, 5]
    assert "examples" not in merge_schemas(a, b, max_examples=0)
    with pytest.raises(ValueError, match="must not be negative"):
        merge_schemas(a, b, max_examples=-1)
    # Examples of other types than the schema allows are dropped.
    odd = merge_schemas({"type": "integer", "examples": [1.5, "x", None, [1], 2]}, {})
    assert odd == {"type": "integer", "examples": [2]}
    widened = merge_schemas({"type": "number", "examples": [1, 2.5, True]}, {})
    assert widened == {"type": "number", "examples": [1, 2.5]}


def test_merge_type_unions() -> None:
    obj = infer_schema([{"a": 1}])
    text = infer_schema(["s"])
    union = merge_schemas(obj, text)
    assert union == {
        "anyOf": [
            {
                "type": "object",
                "properties": {"a": {"type": "integer", "examples": [1]}},
                "required": ["a"],
            },
            {"type": "string", "examples": ["s"]},
        ]
    }
    with_null = merge_schemas(union, {"type": "null"})
    assert with_null["anyOf"] == [*union["anyOf"], {"type": "null"}]  # type: ignore[misc]
    assert merge_schemas({"oneOf": [{"type": "integer"}, {"type": "null"}]}, {}) == {
        "type": ["integer", "null"]
    }


def test_merge_edge_cases() -> None:
    schema = infer_schema([{"a": [1]}])
    assert merge_schemas({}, {}) == {}
    assert merge_schemas({}, schema) == schema
    assert merge_schemas(schema, {}) == schema
    # Unknown types, non-schema branches and unknown keywords are ignored.
    assert merge_schemas({"type": ["string", "file"], "anyOf": None}, {"anyOf": [1, {}]}) == {
        "type": "string"
    }
    # Always-empty arrays merge with arrays that had items.
    assert merge_schemas({"type": "array"}, infer_schema([[1]])) == {
        "type": "array",
        "items": {"type": "integer", "examples": [1]},
    }
    # "required" without a property schema still counts.
    required_only: dict[str, JsonValue] = {"type": "object", "required": ["a", 7]}
    with_property: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"a": {"type": "integer"}},
        "required": ["a"],
    }
    assert merge_schemas(required_only, with_property) == {
        "type": "object",
        "properties": {"a": {"type": "integer"}},
        "required": ["a"],
    }


# ---------------------------------------------------------------------------
# compare_schemas
# ---------------------------------------------------------------------------

PETSTORE: dict[str, JsonValue] = {
    "components": {
        "schemas": {
            "Pet": {
                "required": ["name", "photoUrls"],
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "format": "int64", "example": 10},
                    "name": {"type": "string", "example": "doggie"},
                    "category": {"$ref": "#/components/schemas/Category"},
                    "photoUrls": {
                        "type": "array",
                        "xml": {"wrapped": True},
                        "items": {"type": "string", "xml": {"name": "photoUrl"}},
                    },
                    "tags": {
                        "type": "array",
                        "xml": {"wrapped": True},
                        "items": {"$ref": "#/components/schemas/Tag"},
                    },
                    "status": {
                        "type": "string",
                        "description": "pet status in the store",
                        "enum": ["available", "pending", "sold"],
                    },
                },
                "xml": {"name": "pet"},
            },
            "Category": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "format": "int64", "example": 1},
                    "name": {"type": "string", "example": "Dogs"},
                },
            },
            "Tag": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "format": "int64"},
                    "name": {"type": "string"},
                },
            },
            "Order": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "format": "int64"},
                    "petId": {"type": "integer", "format": "int64"},
                    "quantity": {"type": "integer", "format": "int32"},
                    "shipDate": {"type": "string", "format": "date-time"},
                    "status": {"type": "string", "enum": ["placed", "approved", "delivered"]},
                    "complete": {"type": "boolean"},
                },
            },
            "Node": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
                },
            },
            "Loop": {"allOf": [{"$ref": "#/components/schemas/Loop"}]},
        }
    }
}


def resolve(node: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    ref = node["$ref"]
    assert isinstance(ref, str)
    target: JsonValue = PETSTORE
    for part in ref.removeprefix("#/").split("/"):
        target = _obj(target)[part]
    return _obj(target)


PET_REF: dict[str, JsonValue] = {"$ref": "#/components/schemas/Pet"}


def _kinds(differences: list[SchemaDifference]) -> list[tuple[str, str]]:
    return [(difference.path, difference.kind) for difference in differences]


def test_conforming_pets_have_no_differences() -> None:
    pets = loads(PETS)
    assert isinstance(pets, list)
    named: list[JsonValue] = [
        {key: value for key, value in {**_obj(pet), "name": f"Pet{i}"}.items() if value is not None}
        for i, pet in enumerate(pets)
    ]
    observed = infer_schema(named)
    assert compare_schemas(PET_REF, observed, resolve=resolve) == []
    # The real fixture has "category": null, which the documented Category does not allow.
    (null_category,) = compare_schemas(PET_REF, infer_schema(pets), resolve=resolve)[1:]
    assert (null_category.path, null_category.kind) == ("$.category", "nullability")
    as_list: dict[str, JsonValue] = {"type": "array", "items": PET_REF}
    assert compare_schemas(as_list, infer_schema([named]), resolve=resolve) == []


def test_petstore_differences_in_document_order() -> None:
    pets = loads(
        '[{"id":1,"name":"Pet1","photoUrls":["a"],"status":"available",'
        '"category":{"id":1,"name":"Dogs","extra":true},"tags":[{"id":5,"name":null}]},'
        '{"id":2.5,"status":"sold","weight":3,"photoUrls":[]}]'
    )
    assert isinstance(pets, list)
    differences = compare_schemas(PET_REF, infer_schema(pets), resolve=resolve)
    assert _kinds(differences) == [
        ("$.id", "type_mismatch"),
        ("$.name", "required_mismatch"),
        ("$.category.extra", "undocumented_field"),
        ("$.tags[*].name", "nullability"),
        ("$.weight", "undocumented_field"),
    ]
    by_path = {difference.path: difference for difference in differences}
    assert by_path["$.id"].documented == "integer"
    assert by_path["$.id"].observed == "number"
    assert by_path["$.name"].documented == "required"
    assert by_path["$.name"].observed == "optional"
    assert by_path["$.weight"].documented is None
    assert by_path["$.weight"].observed == {"type": "integer", "examples": [3]}
    assert by_path["$.tags[*].name"].documented == "string"
    assert by_path["$.tags[*].name"].observed == "null"
    assert "not nullable" in by_path["$.tags[*].name"].note

    missing = compare_schemas(PET_REF, infer_schema([{"id": 1}]), resolve=resolve)
    assert _kinds(missing) == [("$.name", "missing_field"), ("$.photoUrls", "missing_field")]
    assert missing[0].documented == {"type": "string", "example": "doggie"}
    assert missing[0].observed is None
    dumped = missing[1].model_dump(mode="json")
    assert dumped["kind"] == "missing_field"
    assert dumped["path"] == "$.photoUrls"


def test_unresolved_references_accept_anything() -> None:
    observed = infer_schema([{"id": "not a number", "unknown": 1}])
    assert compare_schemas(PET_REF, observed) == []

    def broken(node: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        raise KeyError(node["$ref"])

    assert compare_schemas(PET_REF, observed, resolve=broken) == []
    assert compare_schemas({"oneOf": [PET_REF, {"type": "string"}]}, observed) == []


def test_number_documented_accepts_integers_but_not_the_reverse() -> None:
    assert compare_schemas({"type": "number"}, infer_schema([1, 2])) == []
    (difference,) = compare_schemas({"type": "integer"}, infer_schema([1, 2.5]))
    assert difference.kind == "type_mismatch"
    assert difference.path == "$"
    assert difference.note == "observed number, documented integer"


CATEGORY_REF: dict[str, JsonValue] = {"$ref": "#/components/schemas/Category"}


@pytest.mark.parametrize(
    "documented",
    [
        {"type": "string", "nullable": True},  # OpenAPI 3.0
        {"type": "string", "x-nullable": True},  # Swagger 2
        {"type": ["string", "null"]},  # OpenAPI 3.1
        {"oneOf": [{"type": "string"}, {"type": "null"}]},
        {"enum": ["a", "b", None]},
        {"description": "anything goes"},
    ],
)
def test_nullable_documentation(documented: dict[str, JsonValue]) -> None:
    assert compare_schemas(documented, infer_schema(["a", None])) == []


@pytest.mark.parametrize(
    "documented",
    [
        {"allOf": [CATEGORY_REF], "nullable": True},
        {"anyOf": [CATEGORY_REF], "nullable": True},
        {**CATEGORY_REF, "nullable": True},
        {"oneOf": [CATEGORY_REF, {"type": "null"}]},
    ],
)
def test_nullable_references(documented: dict[str, JsonValue]) -> None:
    observed = infer_schema([{"id": 1, "name": "Dogs"}, None])
    assert compare_schemas(documented, observed, resolve=resolve) == []


def test_not_nullable() -> None:
    observed = infer_schema(["a", None])
    strict: list[dict[str, JsonValue]] = [
        {"type": "string"},
        {"type": "string", "nullable": False},
        {"enum": ["a"]},
    ]
    for documented in strict:
        (difference,) = compare_schemas(documented, observed)
        assert difference.kind == "nullability"
        assert difference.documented == "string"
    (wrapped,) = compare_schemas({"allOf": [CATEGORY_REF]}, infer_schema([None]), resolve=resolve)
    assert wrapped.kind == "nullability"
    assert wrapped.documented == "object"


def test_all_of_combines_parts() -> None:
    documented: dict[str, JsonValue] = {
        "allOf": [
            {"$ref": "#/components/schemas/Tag"},
            {"type": "object", "required": ["color"], "properties": {"color": {"type": "string"}}},
            {"properties": {"name": {"minLength": 1}}, "required": ["name"]},
        ]
    }
    observed = infer_schema([{"id": 1, "name": "x"}, {"id": 2, "name": 3, "size": 9}])
    differences = compare_schemas(documented, observed, resolve=resolve)
    assert _kinds(differences) == [
        ("$.name", "type_mismatch"),
        ("$.color", "missing_field"),
        ("$.size", "undocumented_field"),
    ]
    assert differences[0].documented == "string"
    assert differences[0].observed == "integer"
    assert differences[1].documented == {"type": "string"}
    # allOf of incompatible types documents nothing an object can be.
    (conflict,) = compare_schemas(
        {"allOf": [{"type": "string"}, {"type": "integer"}]}, infer_schema([1])
    )
    assert conflict.kind == "type_mismatch"
    assert conflict.documented == []
    narrowed: dict[str, JsonValue] = {
        "allOf": [{"type": ["number", "string"]}, {"type": "integer"}]
    }
    assert compare_schemas(narrowed, infer_schema([1])) == []
    assert _kinds(compare_schemas(narrowed, infer_schema(["s"]))) == [("$", "type_mismatch")]


def test_one_of_uses_the_best_alternative_per_observed_type() -> None:
    either: dict[str, JsonValue] = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    assert compare_schemas(either, infer_schema(["a", 1])) == []
    (mismatch,) = compare_schemas(either, infer_schema([True]))
    assert mismatch.documented == ["string", "integer"]
    assert mismatch.observed == "boolean"

    animals: dict[str, JsonValue] = {
        "anyOf": [
            {"type": "object", "required": ["meows"], "properties": {"meows": {"type": "boolean"}}},
            {"type": "object", "required": ["barks"], "properties": {"barks": {"type": "boolean"}}},
        ]
    }
    assert compare_schemas(animals, infer_schema([{"barks": True}])) == []
    closest = compare_schemas(animals, infer_schema([{"barks": True, "wags": True}]))
    assert _kinds(closest) == [("$.wags", "undocumented_field")]


def test_observed_any_of_against_documented_alternatives() -> None:
    observed = infer_schema([{"a": 1}, [1], None])
    documented: dict[str, JsonValue] = {
        "oneOf": [
            {"type": "object", "properties": {"a": {"type": "integer"}}},
            {"type": "array", "items": {"type": "string"}},
        ]
    }
    assert _kinds(compare_schemas(documented, observed)) == [
        ("$[*]", "type_mismatch"),
        ("$", "nullability"),
    ]


def test_additional_properties() -> None:
    rates = infer_schema([loads(FRANKFURTER)])
    documented: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "amount": {"type": "number"},
            "base": {"type": "string"},
            "date": {"type": "string", "format": "date"},
            "rates": {"type": "object", "additionalProperties": {"type": "number"}},
        },
    }
    assert compare_schemas(documented, rates) == []
    odd = infer_schema([{"rates": {"USD": 1.12, "XAU": "n/a"}}])
    assert _kinds(compare_schemas(documented, odd)) == [("$.rates.XAU", "type_mismatch")]

    closed: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"a": {}},
        "additionalProperties": False,
    }
    (extra,) = compare_schemas(closed, infer_schema([{"a": 1, "b": 2}]))
    assert extra.kind == "undocumented_field"
    assert extra.note == "observed, but not documented (additionalProperties: false)"
    open_schemas: list[dict[str, JsonValue]] = [
        {"type": "object", "properties": {"a": {}}, "additionalProperties": True},
        {"type": "object", "properties": {"a": {}}, "additionalProperties": {}},
        {"type": "object"},  # free-form: nothing about members is documented
        {"type": "object", "required": ["a"]},
    ]
    for open_schema in open_schemas:
        assert compare_schemas(open_schema, infer_schema([{"a": 1, "b": 2}])) == []
    empty: dict[str, JsonValue] = {"type": "object", "properties": {}}
    assert _kinds(compare_schemas(empty, infer_schema([{"b": 2}]))) == [
        ("$.b", "undocumented_field")
    ]
    mixed: dict[str, JsonValue] = {"allOf": [closed, {"additionalProperties": {"type": "integer"}}]}
    assert compare_schemas(mixed, infer_schema([{"a": 1, "b": 2}])) == []
    forbidding: dict[str, JsonValue] = {"allOf": [closed, {"additionalProperties": True}]}
    assert _kinds(compare_schemas(forbidding, infer_schema([{"b": 2}]))) == [
        ("$.b", "undocumented_field")
    ]
    allowing: dict[str, JsonValue] = {
        "allOf": [{"properties": {"a": {}}}, {"additionalProperties": True}]
    }
    assert compare_schemas(allowing, infer_schema([{"b": 2}])) == []


def test_pattern_properties() -> None:
    documented: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"id": {"type": "integer"}},
        "patternProperties": {"^x-": {"type": "string"}, "(": {"type": "string"}},
    }
    observed = infer_schema([{"id": 1, "x-trace": "abc", "x-count": 2, "other": True}])
    assert _kinds(compare_schemas(documented, observed)) == [
        ("$.x-count", "type_mismatch"),
        ("$.other", "undocumented_field"),
    ]


def test_write_only_and_schemaless_required_fields() -> None:
    documented: dict[str, JsonValue] = {
        "type": "object",
        "required": ["username", "password", "token"],
        "properties": {
            "username": {"type": "string"},
            "password": {"type": "string", "writeOnly": True},
        },
    }
    differences = compare_schemas(documented, infer_schema([{"username": "jane"}]))
    assert _kinds(differences) == [("$.token", "missing_field")]
    assert differences[0].documented is None
    assert compare_schemas(documented, infer_schema([{"username": "j", "token": "t"}])) == []
    twice: dict[str, JsonValue] = {
        "allOf": [{"properties": {"a": {"type": "string"}}}, {"properties": {"a": {}}}]
    }
    (missing,) = compare_schemas({**twice, "required": ["a"]}, infer_schema([{}]))
    assert missing.documented == {"allOf": [{"type": "string"}, {}]}


@pytest.mark.parametrize(
    ("documented", "observed", "expected"),
    [
        ({"properties": {"a": {}}}, "text", "object"),
        ({"items": {"type": "string"}}, {"a": 1}, "array"),
        ({"enum": ["a", "b"]}, 1, "string"),
        ({"enum": [1, 2.5]}, "x", ["integer", "number"]),
    ],
)
def test_implied_types(
    documented: dict[str, JsonValue], observed: JsonValue, expected: JsonValue
) -> None:
    (difference,) = compare_schemas(documented, infer_schema([observed]))
    assert difference.kind == "type_mismatch"
    assert difference.documented == expected


def test_format_mismatches() -> None:
    order: dict[str, JsonValue] = {"$ref": "#/components/schemas/Order"}
    observed = infer_schema(
        [
            {"id": 1, "petId": 2, "quantity": 1, "shipDate": "2026-10-11", "complete": False},
            {"id": 2, "shipDate": "2026-10-12", "status": "placed"},
        ]
    )
    (difference,) = compare_schemas(order, observed, resolve=resolve)
    assert difference.kind == "format_mismatch"
    assert difference.path == "$.shipDate"
    assert difference.documented == "date-time"
    assert difference.observed == "date"
    assert difference.note == "documented format date-time, observed date"
    good = infer_schema([{"shipDate": "2026-10-11T01:17:26.844Z"}])
    assert compare_schemas(order, good, resolve=resolve) == []
    (plain,) = compare_schemas({"type": "string", "format": "uuid"}, infer_schema(["abc"]))
    assert plain.observed is None
    assert plain.note == "documented format uuid, observed no recognizable format"
    for undetected in ("int64", "password", "byte"):
        assert compare_schemas({"type": "string", "format": undetected}, infer_schema(["x"])) == []
    assert compare_schemas({"format": "date-time"}, infer_schema([5])) == []


def test_recursive_and_cyclic_schemas() -> None:
    node: dict[str, JsonValue] = {"$ref": "#/components/schemas/Node"}
    tree: JsonValue = {
        "name": "root",
        "children": [{"name": "a", "children": [{"name": "b", "x": 1}]}],
    }
    differences = compare_schemas(node, infer_schema([tree]), resolve=resolve)
    assert _kinds(differences) == [("$.children[*].children[*].x", "undocumented_field")]
    loop: dict[str, JsonValue] = {"$ref": "#/components/schemas/Loop"}
    assert compare_schemas(loop, infer_schema([1]), resolve=resolve) == []
    cyclic: dict[str, JsonValue] = {"type": "object", "properties": {}}
    cyclic["allOf"] = [cyclic]
    assert compare_schemas(cyclic, infer_schema([{"a": 1}])) == []


def test_boolean_schemas_and_empty_observations() -> None:
    documented: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "anything": True,
            "nothing": False,
            "list": {"type": "array", "items": True},
        },
    }
    observed = infer_schema([{"anything": 1, "nothing": 2, "list": [1, "a"]}])
    assert _kinds(compare_schemas(documented, observed)) == [("$.nothing", "type_mismatch")]
    assert compare_schemas(documented, {}) == []
    assert compare_schemas(documented, infer_schema([{"list": []}])) == []
    assert compare_schemas({"type": "array"}, infer_schema([[1]])) == []


def test_missing_field_documentation_is_json() -> None:
    weird: dict[str, JsonValue] = {"type": "string", "example": date(2026, 10, 10)}  # type: ignore[dict-item]
    weird["self"] = weird
    weird["pair"] = (1, 2)  # type: ignore[assignment]
    documented: dict[str, JsonValue] = {
        "type": "object",
        "required": ["when"],
        "properties": {"when": weird},
    }
    (difference,) = compare_schemas(documented, infer_schema([{}]))
    assert difference.documented == {
        "type": "string",
        "example": "2026-10-10",
        "self": "(recursive)",
        "pair": [1, 2],
    }


def test_many_alternatives_are_capped() -> None:
    choices: list[JsonValue] = [{"type": "string", "format": f"f{index}"} for index in range(10)]
    documented: dict[str, JsonValue] = {"allOf": [{"oneOf": choices}, {"oneOf": choices}]}
    assert compare_schemas(documented, infer_schema(["x"])) == []


def test_type_mismatch_mentions_documented_null() -> None:
    (difference,) = compare_schemas({"type": ["string", "null"]}, infer_schema([1, None]))
    assert difference.kind == "type_mismatch"
    assert difference.documented == ["string", "null"]
    assert difference.note == "observed integer, documented ['string', 'null']"


def test_malformed_observed_schemas_are_skipped() -> None:
    documented: dict[str, JsonValue] = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert compare_schemas(documented, {"type": "object", "properties": {"a": 5}}) == []
    assert compare_schemas(documented, {"anyOf": [3, {"type": "null"}]}) == [
        SchemaDifference(
            path="$",
            kind="nullability",
            documented="object",
            observed="null",
            note="observed null, but the documented schema is not nullable",
        )
    ]
