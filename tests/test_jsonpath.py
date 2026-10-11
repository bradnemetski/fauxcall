from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fauxcall.jsonpath import (
    WILDCARD,
    format_path,
    get_all,
    get_one,
    iter_matches,
    leaf_paths,
    parse,
    set_all,
)

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

DOC: JsonValue = {
    "data": [
        {"id": 1, "time": "t1", "tags": ["a", "b"]},
        {"id": 2, "time": "t2", "tags": []},
    ],
    "rates": {"EUR": 0.9, "GBP": 0.75},
    "odd key": {"x.y": 5},
}


@pytest.mark.parametrize(
    ("path", "segments"),
    [
        ("", ()),
        ("$", ()),
        ("a", ("a",)),
        ("$.a.b", ("a", "b")),
        ("a[0]", ("a", 0)),
        ("a[-1]", ("a", -1)),
        ("a[*].b", ("a", WILDCARD, "b")),
        ("a.*", ("a", WILDCARD)),
        ('["odd key"]["x.y"]', ("odd key", "x.y")),
        ("['single']", ("single",)),
        ('["quote \\" inside"]', ('quote " inside',)),
        ("$.@type", ("@type",)),
    ],
)
def test_parse(path: str, segments: tuple[object, ...]) -> None:
    assert parse(path) == segments


@pytest.mark.parametrize("bad", ["a..b", "a[", "a[x]", "a b"])
def test_parse_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid JSON path"):
        parse(bad)


def test_format_path_round_trips() -> None:
    for path in ["$", "$.a.b", "$.a[0]", "$.a[*].b", '$["odd key"]["x.y"]', '$["quo\\"te"]']:
        assert format_path(parse(path)) == path


def test_get_all_and_get_one() -> None:
    assert get_all(DOC, "data[*].time") == ["t1", "t2"]
    assert get_all(DOC, "data[*].tags[*]") == ["a", "b"]
    assert get_all(DOC, "rates.*") == [0.9, 0.75]
    assert get_all(DOC, "data[-1].id") == [2]
    assert get_all(DOC, "data[5].id") == []
    assert get_all(DOC, "missing.path") == []
    assert get_all(DOC, "data.id") == []  # name segment on a list matches nothing
    assert get_all(DOC, '["odd key"]["x.y"]') == [5]
    assert get_one(DOC, "rates.GBP") == 0.75
    assert get_one(DOC, "rates.JPY", default="n/a") == "n/a"
    assert get_one(DOC, "$") == DOC
    assert get_all("scalar", "[*]") == []


def test_iter_matches_reports_concrete_paths() -> None:
    trails = [trail for trail, _ in iter_matches(DOC, "data[*].id")]
    assert trails == [("data", 0, "id"), ("data", 1, "id")]


def test_set_all_updates_every_match() -> None:
    doc: JsonValue = {"data": [{"t": 1}, {"t": 2}, {"x": 3}]}
    assert set_all(doc, "data[*].t", lambda old: f"v{old}") == 2
    assert doc == {"data": [{"t": "v1"}, {"t": "v2"}, {"x": 3}]}
    assert set_all(doc, "nothing.here", lambda old: old) == 0
    with pytest.raises(ValueError, match="root"):
        set_all(doc, "$", lambda old: old)


def test_leaf_paths() -> None:
    leaves = dict(leaf_paths({"a": [{"b": 1}, {"b": 2}], "c": None, "d": {"e f": True}}))
    assert leaves == {"$.a[*].b": 2, "$.c": None, '$.d["e f"]': True}
    concrete = list(leaf_paths({"a": [1, 2]}, generalize_lists=False))
    assert concrete == [("$.a[0]", 1), ("$.a[1]", 2)]
    assert list(leaf_paths(7)) == [("$", 7)]
