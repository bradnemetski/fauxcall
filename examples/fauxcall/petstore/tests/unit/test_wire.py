"""Java/Jackson spelling helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fauxcall_petstore import bodies, handlers, wire

if TYPE_CHECKING:
    from fauxcall.types import JsonValue


def test_java_string_hash_matches_java() -> None:
    assert wire.java_string_hash("") == 0
    assert wire.java_string_hash("placed") == -985774019  # "placed".hashCode() in Java
    assert wire.java_string_hash("approved") == 1185244855


def test_hashmap_order() -> None:
    assert wire.hashmap_order(["placed", "approved"]) == ["approved", "placed"]
    assert wire.hashmap_order([]) == []
    many = [f"s{i}" for i in range(20)]
    assert sorted(wire.hashmap_order(many)) == sorted(many)


def test_java_date_string() -> None:
    moment = datetime(2026, 10, 11, 12, 38, tzinfo=UTC)
    assert wire.java_date_string(int(moment.timestamp() * 1e9)) == "Sun Oct 11 12:38:00 UTC 2026"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-12T10:00:00.000Z", "2026-10-12T10:00:00.000+00:00"),
        ("2026-10-12T12:30:45+02:00", "2026-10-12T10:30:45.000+00:00"),
        ("2026-10-12", "2026-10-12T00:00:00.000+00:00"),
        ("2026-10-12T01:02:03.456789", "2026-10-12T01:02:03.456+00:00"),
    ],
)
def test_ship_dates(text: str, expected: str) -> None:
    moment = wire.parse_date(text)
    assert moment is not None
    assert wire.format_ship_date(moment) == expected


def test_unparseable_dates() -> None:
    assert wire.parse_date("") is None
    assert wire.parse_date("tomorrow") is None
    assert wire.epoch_ms("nope") is None


def test_xml_helpers() -> None:
    assert wire.map_xml({}) == "<HashMap/>"
    assert wire.users_xml([]) == "<User/>"
    assert wire.order_xml({"id": 0, "complete": False}) == (
        "<Order><complete>false</complete><id>0</id><petId/><quantity/><shipDate/><status/></Order>"
    )
    assert wire.pet_xml({"id": 1, "name": "a&b", "category": "odd"}) == (
        "<Pet><id>1</id><category/><name>a&amp;b</name><status/></Pet>"
    )


def test_pet_json_keeps_odd_values() -> None:
    assert wire.pet_json({"id": 1, "tags": ["x"]}) == {"id": 1, "tags": ["x"]}


def test_body_scalars() -> None:
    assert bodies.parse_long("+5") == 5
    assert bodies.parse_long("9223372036854775808") is None
    assert bodies.long_value("") is None
    assert bodies.long_value("1e3") == 1000
    assert bodies.int_value(None) is None
    assert bodies.string_value(1.5) == "1.5"
    assert bodies.string_value(False) == "false"
    assert bodies.bool_value(1) is True
    assert bodies.bool_value("") is False
    bad_values: list[JsonValue] = [{"a": 1}, [1]]
    for bad in bad_values:
        with pytest.raises(bodies.Unreadable):
            bodies.long_value(bad)
        with pytest.raises(bodies.Unreadable):
            bodies.string_value(bad)
    with pytest.raises(bodies.Unreadable):
        bodies.int_value(2**40)
    with pytest.raises(bodies.Unreadable):
        bodies.bool_value([])


def test_body_structures() -> None:
    with pytest.raises(bodies.Unreadable):
        bodies.pet_from({"tags": "x"})
    with pytest.raises(bodies.Unreadable):
        bodies.order_from([])
    with pytest.raises(bodies.Unreadable):
        bodies.user_from("x")
    with pytest.raises(bodies.Unreadable):
        bodies.pet_from_form({"id": "abc"})
    assert bodies.pet_from_form({"id": ""}) == {"id": None, "name": None, "status": None}
    assert bodies.decode_xml(b"<Pet>text</Pet>") == {}
    assert bodies.decode_xml(b"  ") is bodies.ABSENT
    assert bodies.decode_form(b"a=1&a=2&b=") == {"a": "1", "b": ""}
    assert bodies.media_type("Application/JSON; charset=utf-8") == "application/json"


def test_inventory_counts_edge_cases() -> None:
    assert handlers.inventory_counts([]) == {"placed": -20}
    assert handlers.inventory_counts([{"status": "placed"}]) == {"placed": -20}
    assert handlers.inventory_counts([{"quantity": 1}]) is None
