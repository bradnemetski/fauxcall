from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fauxcall.errors import WorldError
from fauxcall.jsonfmt import JsonFloat
from fauxcall.model.world import CollectionModel
from fauxcall.world.keys import (
    DuplicateKeyError,
    InvalidKeyError,
    collection_key,
    ensure_key,
    normalize_key,
    parse_path,
    payload_key,
    record_key,
    set_key,
)

if TYPE_CHECKING:
    from fauxcall.model.world import KeyType
    from fauxcall.types import JsonValue

PETS = CollectionModel(name="pets")
UPLOADS = CollectionModel(name="uploads", key="upload_id", key_type="uuid")
TICKERS = CollectionModel(name="tickers", key="symbol", key_type="str")
NESTED = CollectionModel(name="orders", key="meta.id")
BREEDS = CollectionModel(name="breeds", key="$", key_type="str")

UPLOAD_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


@pytest.mark.parametrize(
    ("value", "key_type", "expected"),
    [
        (42, "int", 42),
        ("42", "int", 42),
        ("-7", "int", -7),
        ("007", "int", 7),
        (42.0, "int", 42),
        (JsonFloat("42.0"), "int", 42),
        (UPLOAD_ID.upper(), "uuid", UPLOAD_ID),
        ("{" + UPLOAD_ID + "}", "uuid", UPLOAD_ID),
        (UPLOAD_ID.replace("-", ""), "uuid", UPLOAD_ID),
        ("AAPL", "str", "AAPL"),
        (42, "str", "42"),
        ("", "str", ""),
    ],
)
def test_normalize_key(value: JsonValue, key_type: KeyType, expected: int | str) -> None:
    assert normalize_key(value, key_type) == expected
    assert type(normalize_key(value, key_type)) is type(expected)


@pytest.mark.parametrize(
    ("value", "key_type"),
    [
        (True, "int"),
        (False, "int"),
        (4.5, "int"),
        (float("nan"), "int"),
        ("4.5", "int"),
        ("abc", "int"),
        ("", "int"),
        (" 42", "int"),
        ("+42", "int"),
        ("1" * 101, "int"),
        (None, "int"),
        ([1], "int"),
        ({"id": 1}, "int"),
        ("not-a-uuid", "uuid"),
        (42, "uuid"),
        (None, "uuid"),
        (True, "str"),
        (1.5, "str"),
        (None, "str"),
        (["AAPL"], "str"),
    ],
)
def test_normalize_key_rejects(value: JsonValue, key_type: KeyType) -> None:
    with pytest.raises(InvalidKeyError, match=f"is not a valid {key_type} key"):
        normalize_key(value, key_type)


def test_key_errors_are_world_errors() -> None:
    assert issubclass(InvalidKeyError, WorldError)
    assert issubclass(DuplicateKeyError, WorldError)


def test_collection_key_names_the_collection() -> None:
    assert collection_key(PETS, "12") == 12
    with pytest.raises(InvalidKeyError, match="collection 'pets': 'abc' is not a valid int key"):
        collection_key(PETS, "abc")


def test_record_key_extracts_and_normalizes() -> None:
    assert record_key(PETS, {"id": "42", "name": "doggie"}) == 42
    assert record_key(UPLOADS, {"upload_id": UPLOAD_ID.upper()}) == UPLOAD_ID
    assert record_key(TICKERS, {"symbol": "AAPL"}) == "AAPL"
    assert record_key(NESTED, {"meta": {"id": 7}}) == 7
    assert record_key(BREEDS, "akita") == "akita"


@pytest.mark.parametrize("record", [{"name": "doggie"}, {"id": None}, [1, 2], "doggie"])
def test_record_key_requires_a_key(record: JsonValue) -> None:
    with pytest.raises(WorldError, match=r"collection 'pets': record has no key at 'id'"):
        record_key(PETS, record)


def test_record_key_rejects_invalid_keys() -> None:
    with pytest.raises(InvalidKeyError, match="'abc' at 'id' is not a valid int key"):
        record_key(PETS, {"id": "abc"})


def test_payload_key_is_none_when_absent() -> None:
    assert payload_key(PETS, {"name": "doggie"}) is None
    assert payload_key(PETS, {"id": None}) is None
    assert payload_key(PETS, {"id": 3}) == 3


def test_set_key_writes_in_place_and_creates_parents() -> None:
    record: JsonValue = {"name": "doggie"}
    assert set_key(PETS, record, 5) is record
    assert record == {"name": "doggie", "id": 5}
    nested: JsonValue = {"meta": None, "total": 3}
    assert set_key(NESTED, nested, 9) == {"meta": {"id": 9}, "total": 3}
    assert set_key(NESTED, {}, 9) == {"meta": {"id": 9}}
    assert set_key(BREEDS, "akita", "beagle") == "beagle"


@pytest.mark.parametrize(
    ("collection", "record"),
    [
        (NESTED, {"meta": "not an object"}),
        (CollectionModel(name="x", key="ids[0]"), {"ids": [1]}),
        (CollectionModel(name="x", key="items[*].id"), {"items": [{"id": 1}]}),
        (CollectionModel(name="x", key="refs[0].id"), {"refs": [{"id": 1}]}),
        (PETS, [1, 2]),
    ],
)
def test_set_key_rejects_unwritable_paths(collection: CollectionModel, record: JsonValue) -> None:
    with pytest.raises(WorldError, match="cannot write a key at"):
        set_key(collection, record, 1)


def test_ensure_key() -> None:
    assert ensure_key(PETS, {"name": "doggie"}, 5) == {"name": "doggie", "id": 5}
    assert ensure_key(PETS, {"id": None}, 5) == {"id": 5}
    same_key_other_spelling: JsonValue = {"id": "5"}
    assert ensure_key(PETS, same_key_other_spelling, 5) is same_key_other_spelling
    assert same_key_other_spelling == {"id": "5"}
    with pytest.raises(WorldError, match="cannot change the key of record 5 to 6"):
        ensure_key(PETS, {"id": 6}, 5)


def test_parse_path_is_cached_and_raises_world_errors() -> None:
    assert parse_path("tags[*].id") is parse_path("tags[*].id")
    with pytest.raises(WorldError, match="invalid JSON path"):
        parse_path("tags[")
    with pytest.raises(WorldError, match="invalid JSON path"):
        record_key(CollectionModel(name="x", key="a..b"), {"a": 1})
