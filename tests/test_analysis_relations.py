from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fauxcall.analysis.relations import (
    MAX_KEY_LENGTH,
    RelationCandidate,
    _identifying,
    _name_hint,
    _scalar_values,
    candidates_table,
    relation_candidates,
)

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

EXCHANGES: JsonValue = {
    "data": [
        {"code": "XNYS", "name": "New York Stock Exchange", "currency": "USD", "open": True},
        {"code": "XNAS", "name": "Nasdaq", "currency": "USD", "open": True},
        {"code": "XLON", "name": "London Stock Exchange", "currency": "GBP", "open": False},
        {"code": "XPAR", "name": "Euronext Paris", "currency": "EUR", "open": None},
    ]
}
TICKERS: JsonValue = {
    "data": [
        {"symbol": "IBM", "exchangeId": "XNYS", "lot": 100},
        {"symbol": "AAPL", "exchangeId": "XNAS", "lot": 100},
        {"symbol": "VOD", "exchangeId": "XLON", "lot": 100},
        {"symbol": "AIR", "exchangeId": "XPAR", "lot": 100},
        {"symbol": "7203", "exchangeId": "XTKS", "lot": 100},
    ]
}


def keys(documents: list[JsonValue]) -> list[str]:
    return sorted(_identifying(_scalar_values(documents), documents))


def find(candidates: list[RelationCandidate], from_field: str, to_field: str) -> RelationCandidate:
    return next(c for c in candidates if c.from_field == from_field and c.to_field == to_field)


def test_tickers_reference_exchanges() -> None:
    candidates = relation_candidates(
        {"GET /v1/exchanges": [EXCHANGES], "GET /v1/tickers": [TICKERS]}
    )
    best = candidates[0]
    assert best == RelationCandidate(
        from_group="GET /v1/tickers",
        from_field="$.data[*].exchangeId",
        to_group="GET /v1/exchanges",
        to_field="$.data[*].code",
        coverage=0.8,
        distinct_values=5,
        missing=["XTKS"],
        name_hint=True,
    )
    reverse = find(candidates, "$.data[*].code", "$.data[*].exchangeId")
    assert reverse.coverage == 1.0
    assert reverse.name_hint is False
    assert candidates.index(reverse) > 0


def test_recording_the_same_listing_twice_keeps_its_keys() -> None:
    assert keys([EXCHANGES, EXCHANGES]) == ["$.data[*].code", "$.data[*].name"]
    candidates = relation_candidates(
        {"GET /v1/exchanges": [EXCHANGES, EXCHANGES], "GET /v1/tickers": [TICKERS, TICKERS]}
    )
    assert find(candidates, "$.data[*].exchangeId", "$.data[*].code").coverage == 0.8


def test_values_repeated_within_a_listing_are_not_keys() -> None:
    found = keys([EXCHANGES])
    assert "$.data[*].currency" not in found
    assert "$.data[*].open" not in found  # booleans and nulls never identify anything
    assert "$.data[*].lot" not in keys([TICKERS])


def test_individual_records_treat_the_whole_group_as_one_sample() -> None:
    records: list[JsonValue] = [
        {"id": 1, "currency": "USD"},
        {"id": 2, "currency": "USD"},
        {"id": 3, "currency": "EUR"},
    ]
    assert keys(records) == ["$.id"]
    # The same detail response recorded twice still identifies its record.
    details: list[JsonValue] = [{"code": "XNYS"}, {"code": "XNYS"}, {"code": "XLON"}]
    assert keys(details) == ["$.code"]
    # Two different records sharing a value: not a key.
    assert keys([{"code": "XNYS", "v": 1}, {"code": "XNYS", "v": 2}]) == ["$.v"]


def test_world_mode_owner_reference() -> None:
    items: list[JsonValue] = [{"id": 1, "owner_id": 10}, {"id": 2, "owner_id": 11}]
    users: list[JsonValue] = [{"id": 10}, {"id": 11}]
    candidates = relation_candidates({"items": items, "users": users})
    owner = find(candidates, "$.owner_id", "$.id")
    assert (owner.from_group, owner.to_group, owner.coverage) == ("items", "users", 1.0)
    assert owner.name_hint is False


def test_coverage_threshold() -> None:
    target: list[JsonValue] = [{"data": [{"code": c} for c in "ABCDE"]}]

    def referencing(values: str) -> list[JsonValue]:
        return [{"rows": [{"ref": v} for v in values]}]

    at_threshold = relation_candidates({"keys": target, "refs": referencing("ABCXY")})
    assert find(at_threshold, "$.rows[*].ref", "$.data[*].code").coverage == 0.6
    below = relation_candidates({"keys": target, "refs": referencing("ABXYZ")})
    assert all(c.from_field != "$.rows[*].ref" for c in below)


def test_missing_values_are_sorted_and_capped_at_five() -> None:
    codes: list[JsonValue] = [{"code": f"K{i:02d}"} for i in range(30)]
    rows: list[JsonValue] = [{"ref": f"K{i:02d}"} for i in range(16)]
    rows += [{"ref": f"Z{i}"} for i in range(7)]
    target: list[JsonValue] = [{"data": codes}]
    refs: list[JsonValue] = [{"rows": rows}]
    candidate = find(
        relation_candidates({"keys": target, "refs": refs}), "$.rows[*].ref", "$.data[*].code"
    )
    assert candidate.distinct_values == 23
    assert candidate.missing == ["Z0", "Z1", "Z2", "Z3", "Z4"]
    assert candidate.coverage == round(16 / 23, 3)


def test_fields_with_a_single_distinct_value_reference_nothing() -> None:
    groups: dict[str, list[JsonValue]] = {
        "keys": [{"data": [{"code": "A"}, {"code": "B"}]}],
        "refs": [{"rows": [{"ref": "A"}, {"ref": "A"}]}],
    }
    assert all(c.from_group != "refs" for c in relation_candidates(groups))
    # ... and a single distinct value is no key either.
    assert keys([{"data": [{"code": "A"}]}]) == []


def test_long_values_are_ignored() -> None:
    long_a, long_b = "a" * (MAX_KEY_LENGTH + 1), "b" * (MAX_KEY_LENGTH + 1)
    exact_a, exact_b = "c" * MAX_KEY_LENGTH, "d" * MAX_KEY_LENGTH
    documents: list[JsonValue] = [
        {
            "data": [
                {"long": long_a, "exact": exact_a, "empty": ""},
                {"long": long_b, "exact": exact_b, "empty": ""},
            ]
        }
    ]
    assert keys(documents) == ["$.data[*].exact"]


def test_a_field_does_not_reference_itself() -> None:
    # code -> code would have full coverage; the only other overlaps are empty.
    assert relation_candidates({"GET /v1/exchanges": [EXCHANGES]}) == []


def test_self_relations_inside_one_group() -> None:
    nodes: list[JsonValue] = [
        {
            "data": [
                {"id": "n1", "parent": "n1"},
                {"id": "n2", "parent": "n1"},
                {"id": "n3", "parent": "n2"},
            ]
        }
    ]
    candidates = relation_candidates({"GET /v1/nodes": nodes})
    parent = find(candidates, "$.data[*].parent", "$.data[*].id")
    assert parent.from_group == parent.to_group == "GET /v1/nodes"
    assert parent.coverage == 1.0


def test_sorting_name_hint_then_coverage_then_size() -> None:
    groups: dict[str, list[JsonValue]] = {
        "GET /v1/exchanges": [{"data": [{"code": c} for c in ("A", "B", "C", "D")]}],
        "GET /v1/things": [
            {
                "data": [
                    {"exchange": "A", "other": "A", "small": "A"},
                    {"exchange": "B", "other": "B", "small": "B"},
                    {"exchange": "Q", "other": "C", "small": "A"},
                    {"exchange": "R", "other": "D", "small": "B"},
                    {"exchange": "C", "other": "Z", "small": "A"},
                ]
            }
        ],
    }
    candidates = [c for c in relation_candidates(groups) if c.to_field == "$.data[*].code"]
    assert [(c.from_field, c.name_hint, c.coverage) for c in candidates] == [
        ("$.data[*].exchange", True, 0.6),
        ("$.data[*].small", False, 1.0),
        ("$.data[*].other", False, 0.8),
    ]


@pytest.mark.parametrize(
    ("field", "group", "target", "expected"),
    [
        ("$.data[*].exchange", "GET /v1/exchanges", "$.data[*].code", True),
        ("$.data[*].exchangeId", "GET /v1/exchanges/{code}", "$.code", True),
        ("$.data[*].ExchangeID", "exchanges", "$.code", True),
        ("$.data[*].exchange_code", "exchanges", "$.code", True),
        ("$.data[*].exchangeid", "exchanges", "$.code", True),
        ("$.listing.exchanges[*]", "exchanges", "$.code", True),
        ("$.primary_exchange", "exchanges", "$.code", True),
        ("$.exchange", "stock exchanges", "$.mic", True),
        ("$.venue", "GET /trading-venues", "$.mic", True),
        ("$.provider", "providers", "$.id", True),
        ("$.provider_ids[*]", "GET /providers", "$[*].id", True),
        ("$.code", "GET /v1/markets", "$[*].code", True),
        ("$.id", "GET /users", "$[*].id", False),
        ("$.ids[*]", "GET /users", "$[*].id", False),
        ("$.owner_id", "GET /users", "$[*].id", False),
        ("$.valid", "GET /values", "$[*].id", False),
        ("$.ex", "exchanges", "$.code", False),
        ("$[*]", "GET /exchanges", "$[*].code", False),
        ("$.exchange", "GET /", "$[*].code", False),
        ("$.symbol", "MCP tools/call:get_quote", "$.symbol", True),
    ],
)
def test_name_hints(field: str, group: str, target: str, expected: bool) -> None:
    assert _name_hint(field, group, target) is expected


def test_candidates_table() -> None:
    candidates = relation_candidates(
        {"GET /v1/exchanges": [EXCHANGES], "GET /v1/tickers": [TICKERS]}
    )
    lines = candidates_table(candidates).splitlines()
    assert lines[0] == (
        "| from | field | to | key field | coverage | distinct | name hint | missing |"
    )
    assert lines[1] == "|---|---|---|---|---|---|---|---|"
    assert lines[2] == (
        "| GET /v1/tickers | `$.data[*].exchangeId` | GET /v1/exchanges | `$.data[*].code` "
        "| 80% | 5 | yes | XTKS |"
    )
    assert any(line.endswith("| 100% | 4 |  | - |") for line in lines[3:])
    limited = candidates_table(candidates, limit=1).splitlines()
    assert len(limited) == 3
    assert candidates_table([]).count("\n") == 1
