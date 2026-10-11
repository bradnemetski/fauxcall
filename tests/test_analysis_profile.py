from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fauxcall.analysis.profile import (
    FieldProfile,
    looks_like_id_name,
    profile_documents,
    profile_table,
    time_format_summary,
)
from fauxcall.jsonfmt import JsonFloat, loads
from fauxcall.jsonpath import leaf_paths
from fauxcall.timefmt import TimeFormat

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

COINBASE = (
    '{"ask":"82940.02","bid":"82940.01","volume":"1509.40500051","trade_id":1104620943,'
    '"price":"82940.02","size":"0.000012","time":"2026-10-11T01:17:28.087060570Z",'
    '"rfq_volume":"28.844121"}'
)
COINBASE_LATER = (
    '{"ask":"82941.50","bid":"82941.49","volume":"1510.1","trade_id":1104620950,'
    '"price":"82941.5","size":"0.5","time":"2026-10-11T01:17:29.000000001Z",'
    '"rfq_volume":"28.9"}'
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
    '{"id":2,"title":"Activity 2","dueDate":"2026-10-11T03:17:26.8440245Z","completed":true},'
    '{"id":3,"title":"Activity 3","dueDate":"2026-10-11T04:17:26.8440261Z","completed":false}]'
)
FRANKFURTER = (
    '{"amount":1.0,"base":"EUR","date":"2026-10-09","rates":{"GBP":0.84023,"USD":1.1206}}',
    '{"amount":1.0,"base":"EUR","date":"2026-10-10","rates":{"GBP":0.84100,"USD":1.1210}}',
)


def _by_path(profiles: list[FieldProfile]) -> dict[str, FieldProfile]:
    return {profile.path: profile for profile in profiles}


def _docs(*texts: str) -> list[JsonValue]:
    return [loads(text) for text in texts]


# ---------------------------------------------------------------------------
# Realistic payloads
# ---------------------------------------------------------------------------


def test_coinbase_ticker_fields() -> None:
    profiles = profile_documents(_docs(COINBASE))
    assert [p.path for p in profiles] == [
        "$.ask",
        "$.bid",
        "$.volume",
        "$.trade_id",
        "$.price",
        "$.size",
        "$.time",
        "$.rfq_volume",
    ]
    fields = _by_path(profiles)
    assert fields["$.ask"] == FieldProfile(
        path="$.ask",
        documents=1,
        occurrences=1,
        types={"string": 1},
        distinct=1,
        examples=["82940.02"],
        minimum=82940.02,
        maximum=82940.02,
        min_length=8,
        max_length=8,
        decimals=2,
        numeric_string=True,
        cardinality=1.0,
    )
    assert fields["$.volume"].decimals == 8
    assert fields["$.size"].decimals == 6
    trade_id = fields["$.trade_id"]
    assert trade_id.looks_like_id
    assert trade_id.time_format is None  # in the epoch-seconds range, but it is an id
    assert trade_id.decimals is None
    assert not trade_id.numeric_string
    time = fields["$.time"]
    assert time.time_format == TimeFormat(kind="iso", fraction_digits=9)
    assert time.string_format is None
    assert not time.numeric_string
    assert time.min_length == time.max_length == 30


def test_coinbase_ticker_over_time() -> None:
    fields = _by_path(profile_documents(_docs(COINBASE, COINBASE_LATER)))
    ask = fields["$.ask"]
    assert (ask.documents, ask.occurrences, ask.distinct) == (2, 2, 2)
    assert ask.examples == ["82940.02", "82941.50"]
    assert (ask.minimum, ask.maximum) == (82940.02, 82941.5)
    assert ask.decimals == 2
    assert not ask.constant
    assert fields["$.size"].decimals == 6
    assert fields["$.time"].time_format == TimeFormat(kind="iso", fraction_digits=9)
    assert fields["$.trade_id"].looks_like_id


def test_timeapi_time_fields() -> None:
    fields = _by_path(profile_documents(_docs(TIMEAPI)))
    assert fields["$.date_time"].time_format == TimeFormat(
        kind="iso", fraction_digits=6, tz_style="+HH:MM"
    )
    assert fields["$.date"].time_format == TimeFormat(kind="date")
    assert fields["$.time"].time_format == TimeFormat(kind="time", fraction_digits=6)
    for name in ("day_of_week", "timezone", "dst_active", "utc_offset_seconds"):
        assert fields[f"$.{name}"].time_format is None
    dst = fields["$.dst_active"]
    assert dst.types == {"boolean": 1}
    assert dst.minimum is None
    assert dst.examples == [True]
    offset = fields["$.utc_offset_seconds"]
    assert (offset.minimum, offset.maximum) == (-14400.0, -14400.0)
    assert not offset.looks_like_id  # a single integer proves nothing


def test_petstore_pets_with_missing_fields_and_empty_arrays() -> None:
    pets = loads(PETS)
    assert isinstance(pets, list)
    profiles = profile_documents(pets)
    assert [p.path for p in profiles] == [
        "$.id",
        "$.name",
        "$.photoUrls[*]",
        "$.tags",
        "$.status",
        "$.category.id",
        "$.category.name",
        "$.photoUrls",
        "$.tags[*].id",
        "$.tags[*].name",
        "$.category",
    ]
    fields = _by_path(profiles)
    pet_id = fields["$.id"]
    assert (pet_id.documents, pet_id.distinct, pet_id.cardinality) == (3, 3, 1.0)
    assert pet_id.looks_like_id
    assert fields["$.name"].documents == 2
    tags = fields["$.tags"]
    assert (tags.types, tags.examples, tags.documents) == ({"array": 1}, [[]], 1)
    assert fields["$.photoUrls"].types == {"array": 1}
    status = fields["$.status"]
    assert status.examples == ["available", "sold"]
    assert status.cardinality == pytest.approx(2 / 3)
    category = fields["$.category"]
    assert (category.types, category.nulls, category.distinct) == ({"null": 1}, 1, 0)
    assert category.examples == []
    assert category.cardinality == 0.0
    assert fields["$.photoUrls[*]"].string_format is None  # "a" is not a URL
    assert fields["$.tags[*].id"].looks_like_id


def test_fakerestapi_activities() -> None:
    fields = _by_path(profile_documents(_docs(ACTIVITIES)))
    assert list(fields) == ["$[*].id", "$[*].title", "$[*].dueDate", "$[*].completed"]
    due = fields["$[*].dueDate"]
    assert (due.documents, due.occurrences, due.distinct) == (1, 3, 3)
    assert due.time_format == TimeFormat(kind="iso", fraction_digits=7)
    activity_id = fields["$[*].id"]
    assert activity_id.looks_like_id
    assert (activity_id.minimum, activity_id.maximum) == (1.0, 3.0)
    completed = fields["$[*].completed"]
    assert completed.examples == [False, True]
    assert completed.distinct == 2
    assert fields["$[*].title"].time_format is None


def test_frankfurter_rates_keep_number_spellings() -> None:
    fields = _by_path(profile_documents(_docs(*FRANKFURTER)))
    amount = fields["$.amount"]
    assert amount.constant
    assert amount.decimals == 1
    (example,) = amount.examples
    assert isinstance(example, JsonFloat)
    assert example.lexeme == "1.0"
    assert fields["$.base"].constant
    assert fields["$.date"].time_format == TimeFormat(kind="date")
    usd = fields["$.rates.USD"]
    assert usd.decimals == 4  # "1.1210" keeps its trailing zero
    assert [e.lexeme for e in usd.examples if isinstance(e, JsonFloat)] == ["1.1206", "1.1210"]
    assert fields["$.rates.GBP"].decimals == 5


# ---------------------------------------------------------------------------
# Shapes and edge cases
# ---------------------------------------------------------------------------


def test_nested_arrays_and_empty_containers() -> None:
    doc: JsonValue = {"matrix": [[1, 2], [3]], "grid": [[]], "meta": {}, "odd key": {"a.b": 1}}
    fields = _by_path(profile_documents([doc]))
    assert list(fields) == ["$.matrix[*][*]", "$.grid[*]", "$.meta", '$["odd key"]["a.b"]']
    assert fields["$.matrix[*][*]"].occurrences == 3
    assert fields["$.grid[*]"].types == {"array": 1}
    assert fields["$.meta"].examples == [{}]


def test_root_documents() -> None:
    assert profile_documents([]) == []
    (scalar,) = profile_documents([5, 7])
    assert (scalar.path, scalar.documents, scalar.occurrences) == ("$", 2, 2)
    (empty,) = profile_documents([{}])
    assert (empty.path, empty.types) == ("$", {"object": 1})
    (array,) = profile_documents([[]])
    assert array.types == {"array": 1}


def test_mixed_types_and_nulls() -> None:
    docs: list[JsonValue] = [{"v": 1}, {"v": "a"}, {"v": None}, {"v": 1.5}, {"v": True}]
    (field,) = profile_documents(docs)
    assert field.types == {"integer": 1, "string": 1, "null": 1, "number": 1, "boolean": 1}
    assert (field.nulls, field.distinct, field.occurrences) == (1, 4, 5)
    assert field.examples == [1, "a", 1.5, True]
    assert field.cardinality == 0.8
    assert (field.minimum, field.maximum) == (1.0, 1.5)
    assert field.decimals == 1
    assert not field.numeric_string
    assert field.time_format is None
    assert not field.constant
    assert not field.looks_like_id


def test_distinct_values_use_json_equality() -> None:
    (field,) = profile_documents([{"v": 1}, {"v": 1.0}, {"v": True}, {"v": None}])
    assert field.distinct == 2
    assert field.examples == [1, True]
    (repeated,) = profile_documents([{"v": "x"}, {"v": "x"}, {"v": None}])
    assert repeated.constant
    (once,) = profile_documents([{"v": "x"}, {"v": None}])
    assert not once.constant  # a single non-null value proves nothing
    (both,) = profile_documents([{"v": []}, {"v": 1}, {"v": []}])
    assert (both.types, both.distinct, both.examples) == ({"array": 2, "integer": 1}, 2, [[], 1])


def test_examples_are_capped() -> None:
    (field,) = profile_documents([{"v": index} for index in range(10)], max_examples=2)
    assert field.examples == [0, 1]
    (none,) = profile_documents([{"v": 1}], max_examples=0)
    assert none.examples == []
    with pytest.raises(ValueError, match="must not be negative"):
        profile_documents([], max_examples=-1)
    with pytest.raises(TypeError, match="is not a JSON value"):
        profile_documents([{"v": (1, 2)}])  # type: ignore[dict-item]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("id", True),
        ("ID", True),
        ("Id", True),
        ("_id", True),
        ("trade_id", True),
        ("TRADE_ID", True),
        ("user-id", True),
        ("petId", True),
        ("userID", True),
        ("tagIds", True),
        ("tag_ids", True),
        ("ids", True),
        ("uuid", True),
        ("order_uuid", True),
        ("guid", True),
        ("key", True),
        ("code", True),
        ("Symbol", True),
        ("valid", False),
        ("paid", False),
        ("PAID", False),
        ("android", False),
        ("identity", False),
        ("idle", False),
        ("keys", False),
        ("zip_code", False),
        ("name", False),
        (None, False),
    ],
)
def test_id_names(name: str | None, expected: bool) -> None:
    assert looks_like_id_name(name) is expected


def test_id_values() -> None:
    uuids: list[JsonValue] = [
        {"ref": "3fa85f64-5717-4562-b3fc-2c963f66afa6"},
        {"ref": "7c9e6679-7425-40de-944b-e07fc1f90ae7"},
    ]
    (ref,) = profile_documents(uuids)
    assert ref.string_format == "uuid"
    assert ref.looks_like_id
    (count,) = profile_documents([{"count": 3}, {"count": 4}, {"count": None}])
    assert count.looks_like_id  # integers that never repeat
    assert not profile_documents([{"count": 3}, {"count": 3}])[0].looks_like_id
    assert not profile_documents([{"count": 3}])[0].looks_like_id
    assert not profile_documents([{"count": 3}, {"count": 4.5}])[0].looks_like_id
    assert not profile_documents([{"flag": True}, {"flag": False}])[0].looks_like_id


def test_epoch_timestamps() -> None:
    seconds: list[JsonValue] = [{"created": 1791681447}, {"created": 1791681448}]
    (created,) = profile_documents(seconds)
    assert created.time_format == TimeFormat(kind="epoch", epoch_unit="s")
    assert not created.looks_like_id  # never-repeating integers, but they are times
    (trade_id,) = profile_documents([{"trade_id": 1791681447}, {"trade_id": 1791681448}])
    assert trade_id.time_format is None
    assert trade_id.looks_like_id
    (millis,) = profile_documents([{"t": 1791681447123}])
    assert millis.time_format == TimeFormat(kind="epoch", epoch_unit="ms")
    (text,) = profile_documents([{"time": "1791681447"}])
    assert text.time_format == TimeFormat(kind="epoch", as_string=True)
    assert text.numeric_string
    assert text.decimals == 0


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["a@example.com", "b@example.org"], "email"),
        (["https://a.example.com", "http://b.example.com/x"], "uri"),
        (["10.0.0.1"], "ipv4"),
        (["2001:db8::1"], "ipv6"),
        (["a@example.com", "plain"], None),
        (["plain", "a@example.com"], None),
    ],
)
def test_string_formats(values: list[str], expected: str | None) -> None:
    (field,) = profile_documents([{"v": value} for value in values])
    assert field.string_format == expected


def test_numeric_strings_and_decimals() -> None:
    (codes,) = profile_documents([{"v": "007"}, {"v": "12"}])
    assert not codes.numeric_string  # a leading zero spells a code, not a number
    assert codes.decimals is None
    assert (codes.minimum, codes.maximum) == (None, None)
    assert (codes.min_length, codes.max_length) == (2, 3)
    (negative,) = profile_documents([{"v": "-0.5"}, {"v": "12"}])
    assert negative.numeric_string
    assert (negative.minimum, negative.maximum, negative.decimals) == (-0.5, 12.0, 1)
    (mixed,) = profile_documents([{"v": 2.25}, {"v": "1.5"}])
    assert (mixed.minimum, mixed.maximum, mixed.decimals) == (1.5, 2.25, 2)
    floats: list[JsonValue] = [{"v": 0.25}, {"v": 1e-05}, {"v": JsonFloat("1.5e-7")}]
    (plain,) = profile_documents(floats)
    assert plain.decimals == 2
    (integers,) = profile_documents([{"v": 1}, {"v": 2}])
    assert integers.decimals is None


def test_huge_integers_do_not_overflow() -> None:
    (field,) = profile_documents([{"v": 10**400}, {"v": -(10**400)}])
    assert (field.minimum, field.maximum) == (-math.inf, math.inf)


def test_profiles_round_trip_through_json() -> None:
    profiles = profile_documents(_docs(COINBASE, *FRANKFURTER))
    for profile in profiles:
        dumped = profile.model_dump(mode="json")
        assert FieldProfile.model_validate(dumped) == profile


def test_large_and_deep_inputs() -> None:
    tickers: list[JsonValue] = [
        {
            "trade_id": index,
            "price": f"{80000 + index / 100:.2f}",
            "time": f"2026-10-11T01:{index // 60 % 60:02d}:{index % 60:02d}.{index:09d}Z",
        }
        for index in range(10_000)
    ]
    fields = _by_path(profile_documents(tickers))
    assert fields["$.trade_id"].distinct == 10_000
    assert fields["$.price"].decimals == 2
    assert fields["$.time"].time_format == TimeFormat(kind="iso", fraction_digits=9)
    deep: JsonValue = "leaf"
    for _ in range(5_000):
        deep = [deep]
    (field,) = profile_documents([deep])
    assert field.path == "$" + "[*]" * 5_000


_KEYS = st.sampled_from(["a", "b c", 'q"uote', "x.y", "id", "_k", "ü", "1st"])
_LEAVES = st.none() | st.booleans() | st.integers(-5, 5) | st.text(alphabet="ab", max_size=2)
_DOCS = st.recursive(
    _LEAVES,
    lambda children: (
        st.lists(children, min_size=1, max_size=3)
        | st.dictionaries(_KEYS, children, min_size=1, max_size=3)
    ),
    max_leaves=12,
)


@settings(max_examples=200, deadline=None)
@given(st.lists(_DOCS, max_size=4))
def test_paths_match_leaf_paths(documents: list[JsonValue]) -> None:
    expected: dict[str, int] = {}
    for document in documents:
        for path, _ in leaf_paths(document):
            expected[path] = expected.get(path, 0) + 1
    profiles = profile_documents(documents)
    assert {p.path: p.occurrences for p in profiles} == expected
    assert [p.path for p in profiles] == list(expected)


# ---------------------------------------------------------------------------
# Markdown table
# ---------------------------------------------------------------------------

HEADER = ["| path | types | null% | distinct | examples | notes |", "|---|---|---|---|---|---|"]


def test_table_for_a_coinbase_ticker() -> None:
    table = profile_table(profile_documents(_docs(COINBASE)))
    assert table.splitlines() == [
        *HEADER,
        '| $.ask | string | 0% | 1 | "82940.02" | numeric-string 2dp |',
        '| $.bid | string | 0% | 1 | "82940.01" | numeric-string 2dp |',
        '| $.volume | string | 0% | 1 | "1509.40500051" | numeric-string 8dp |',
        "| $.trade_id | integer | 0% | 1 | 1104620943 | id? |",
        '| $.price | string | 0% | 1 | "82940.02" | numeric-string 2dp |',
        '| $.size | string | 0% | 1 | "0.000012" | numeric-string 6dp |',
        '| $.time | string | 0% | 1 | "2026-10-11T01:17:28.087060570Z" | iso 9f Z |',
        '| $.rfq_volume | string | 0% | 1 | "28.844121" | numeric-string 6dp |',
    ]


def test_table_for_frankfurter_and_petstore() -> None:
    rates = profile_table(profile_documents(_docs(*FRANKFURTER))).splitlines()
    assert rates[2:] == [
        "| $.amount | number | 0% | 1 | 1.0 | 1dp, constant |",
        '| $.base | string | 0% | 1 | "EUR" | constant |',
        '| $.date | string | 0% | 2 | "2026-10-09", "2026-10-10" | date |',
        "| $.rates.GBP | number | 0% | 2 | 0.84023, 0.84100 | 5dp, range 0.84023..0.841 |",
        "| $.rates.USD | number | 0% | 2 | 1.1206, 1.1210 | 4dp, range 1.1206..1.121 |",
    ]
    pets = loads(PETS)
    assert isinstance(pets, list)
    rows = profile_table(profile_documents(pets)).splitlines()
    assert "| $.id | integer | 0% | 3 | 1, 2, 3 | range 1..3, id? |" in rows
    assert '| $.name | string | 0% | 2 | "Pet1", "Pet2" | in 2/3 docs |' in rows
    assert "| $.tags | array | 0% | 1 | [] | in 1/3 docs |" in rows
    assert "| $.category | null | 100% | 0 |  | in 1/3 docs |" in rows


@pytest.mark.parametrize(
    ("nulls", "occurrences", "expected"),
    [
        (0, 5, "0%"),
        (1, 200, "<1%"),
        (1, 3, "33%"),
        (199, 200, ">99%"),
        (4, 4, "100%"),
        (1, 0, "0%"),
    ],
)
def test_table_null_percentages(nulls: int, occurrences: int, expected: str) -> None:
    profile = FieldProfile(path="$.v", documents=1, nulls=nulls, occurrences=occurrences)
    assert profile_table([profile]).splitlines()[2].split(" | ")[2] == expected


def test_table_cells_are_escaped_and_examples_truncated() -> None:
    profiles = [
        FieldProfile(
            path='$["a|b"]', documents=2, types={"string": 3, "null": 1}, examples=["x|y"]
        ),
        FieldProfile(path='$["line\nbreak"]', documents=2, examples=["x" * 60]),
        FieldProfile(
            path="$.t",
            documents=2,
            examples=["2026-10-11T01:17:28.087060570Z", "2026-10-11T01:17:29.000000001Z"],
        ),
    ]
    assert profile_table(profiles).splitlines()[2:] == [
        '| $["a\\|b"] | string 3, null 1 | 0% | 0 | "x\\|y" |  |',
        '| $["line break"] |  | 0% | 0 | "' + "x" * 38 + "… |  |",
        '| $.t |  | 0% | 0 | "2026-10-11T01:17:28.087060570Z", … |  |',
    ]


def test_table_notes() -> None:
    profile = FieldProfile(
        path="$.ref",
        documents=1,
        occurrences=2,
        types={"string": 2},
        distinct=1,
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
        constant=True,
        string_format="uuid",
        looks_like_id=True,
    )
    other = FieldProfile(path="$.n", documents=3, occurrences=3, types={"integer": 3})
    rows = profile_table([profile, other]).splitlines()
    assert rows[2].endswith("| uuid, id?, constant, in 1/3 docs |")
    assert rows[3].endswith("|  |")


def test_table_row_limit() -> None:
    profiles = [FieldProfile(path=f"$.f{index}", documents=1) for index in range(3)]
    limited = profile_table(profiles, max_rows=2).splitlines()
    assert limited[-2:] == ["", "(1 more field not shown; max_rows=2)"]
    assert len(limited) == 6
    assert profile_table(profiles, max_rows=0).splitlines()[-1] == (
        "(3 more fields not shown; max_rows=0)"
    )
    assert "more" not in profile_table(profiles, max_rows=3)
    assert profile_table([]) == "(no fields)"
    with pytest.raises(ValueError, match="must not be negative"):
        profile_table(profiles, max_rows=-1)


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        (TimeFormat(kind="iso", fraction_digits=7), "iso 7f Z"),
        (TimeFormat(kind="iso", fraction_digits=6, tz_style="+HH:MM"), "iso 6f +HH:MM"),
        (
            TimeFormat(kind="iso", separator=" ", seconds=False, tz_style="none"),
            "iso space no-seconds naive",
        ),
        (TimeFormat(kind="iso", fraction_digits=3, fraction_mode="trim"), "iso 3f trim Z"),
        (
            TimeFormat(
                kind="iso", fraction_digits=6, fraction_mode="omit_if_zero", tz_style="none"
            ),
            "iso 6f omit0 naive",
        ),
        (TimeFormat(kind="date"), "date"),
        (TimeFormat(kind="time", fraction_digits=6), "time 6f"),
        (TimeFormat(kind="time", seconds=False), "time no-seconds"),
        (TimeFormat(kind="epoch"), "epoch s"),
        (
            TimeFormat(kind="epoch", epoch_unit="ms", epoch_decimals=3, as_string=True),
            "epoch ms 3dp string",
        ),
        (TimeFormat(kind="http_date"), "http-date"),
        (TimeFormat(kind="strftime", pattern="%m/%d/%Y"), "strftime %m/%d/%Y"),
    ],
)
def test_time_format_summary(fmt: TimeFormat, expected: str) -> None:
    assert time_format_summary(fmt) == expected
