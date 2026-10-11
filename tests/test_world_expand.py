from __future__ import annotations

import itertools
import random
import re
import string
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fauxcall.errors import WorldError
from fauxcall.jsonfmt import JsonFloat
from fauxcall.jsonpath import get_all
from fauxcall.model import (
    ApiModel,
    CollectionModel,
    RealTarget,
    RelationModel,
    SourceInfo,
    WorldRecord,
)
from fauxcall.model.world import ExpansionSpec
from fauxcall.world import (
    World,
    check_world,
    expand_collection,
    load_collection,
    records_from_values,
    resolve_relation_targets,
    save_collection,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonValue

NOW = datetime(2026, 10, 10, 14, 30, tzinfo=UTC)

PETS = CollectionModel(name="pets")
PET_VALUES: list[JsonValue] = [
    {
        "id": 1,
        "name": "doggie",
        "category": {"id": 1, "name": "Dogs"},
        "tags": [{"id": 1, "name": "friendly"}],
        "status": "available",
        "weight": JsonFloat("12.50"),
    },
    {
        "id": 5,
        "name": "kitty",
        "category": {"id": 2, "name": "Cats"},
        "tags": [{"id": 2, "name": "calm"}, {"id": 3, "name": "indoor"}],
        "status": "pending",
        "weight": JsonFloat("4.20"),
    },
    {
        "id": 3,
        "name": "rex",
        "category": {"id": 1, "name": "Dogs"},
        "tags": [],
        "status": "sold",
        "weight": JsonFloat("30.00"),
    },
]
TICKERS = CollectionModel(name="tickers", key="symbol", key_type="str")
TICKER_VALUES: list[JsonValue] = [
    {"symbol": "AAPL", "exchange": "XNAS", "price": JsonFloat("105.50")},
    {"symbol": "IBM", "exchange": "XNYS", "price": JsonFloat("187.00")},
    {"symbol": "MSFT", "exchange": "XNAS", "price": JsonFloat("411.20")},
]
UPLOADS = CollectionModel(name="uploads", key="upload_id", key_type="uuid")
REPORT_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
SCAN_ID = "9b2e4c1a-0d6f-4e8b-a1c3-5f7e9d2b4a60"


def _pets() -> list[WorldRecord]:
    return records_from_values(PETS, PET_VALUES, recorded_at=NOW, source="GET /pet#1")


def _payload(record: WorldRecord) -> dict[str, JsonValue]:
    assert isinstance(record.record, dict)
    return record.record


def test_int_keys_continue_after_the_max_and_templates_rotate() -> None:
    originals = _pets()
    expanded = expand_collection(PETS, originals, ExpansionSpec(target_count=8))
    assert expanded[:3] == originals
    assert all(a is b for a, b in zip(expanded, originals, strict=False))
    synthetic = expanded[3:]
    assert [record.key for record in synthetic] == [6, 7, 8, 9, 10]
    assert [_payload(record)["id"] for record in synthetic] == [6, 7, 8, 9, 10]
    assert [_payload(record)["name"] for record in synthetic] == [
        "doggie",
        "kitty",
        "rex",
        "doggie",
        "kitty",
    ]
    assert all(record.synthetic for record in synthetic)
    assert all(record.recorded_at == NOW for record in synthetic)
    assert all(record.source == "expansion:GET /pet#1" for record in synthetic)
    assert not any(record.synthetic for record in originals)
    assert _payload(originals[0])["id"] == 1  # templates are copied, never modified
    weight = _payload(synthetic[0])["weight"]
    assert isinstance(weight, JsonFloat)
    assert weight.lexeme == "12.50"


def test_vary_samples_observed_values_deterministically() -> None:
    spec = ExpansionSpec(target_count=40, seed=7, vary=["status", "tags[*].name", "nickname"])
    expanded = expand_collection(PETS, _pets(), spec)
    synthetic = expanded[3:]
    statuses = {_payload(record)["status"] for record in synthetic}
    assert statuses == {"available", "pending", "sold"}
    tag_names = {name for record in synthetic for name in get_all(record.record, "tags[*].name")}
    assert tag_names <= {"friendly", "calm", "indoor"}
    assert len(tag_names) > 1
    assert all("nickname" not in _payload(record) for record in synthetic)  # never observed
    assert {_payload(record)["name"] for record in synthetic} == {"doggie", "kitty", "rex"}

    assert expand_collection(PETS, _pets(), spec) == expanded
    other_seed = expand_collection(PETS, _pets(), spec.model_copy(update={"seed": 8}))
    assert [record.key for record in other_seed] == [record.key for record in expanded]
    assert other_seed != expanded


def test_sampled_values_are_independent_copies() -> None:
    spec = ExpansionSpec(target_count=6, vary=["category"])
    expanded = expand_collection(PETS, _pets(), spec)
    categories = [_payload(record)["category"] for record in expanded]
    assert all(isinstance(category, dict) for category in categories)
    assert len({id(category) for category in categories}) == 6


def test_varying_the_key_keeps_keys_unique() -> None:
    expanded = expand_collection(PETS, _pets(), ExpansionSpec(target_count=10, vary=["id"]))
    keys = [record.key for record in expanded]
    assert keys == [1, 5, 3, 6, 7, 8, 9, 10, 11, 12]
    assert [_payload(record)["id"] for record in expanded] == keys


def test_nothing_to_do_when_the_target_is_reached() -> None:
    originals = _pets()
    for target in (2, 3):
        expanded = expand_collection(
            PETS, originals, ExpansionSpec(target_count=target, vary=["$"])
        )
        assert expanded == originals
        assert all(a is b for a, b in zip(expanded, originals, strict=True))


@pytest.mark.parametrize(
    ("records", "vary", "message"),
    [
        ([], [], "collection 'pets': cannot expand an empty collection"),
        (None, ["$"], r"cannot vary the whole record \('\$'\)"),
        (None, ["tags["], "invalid JSON path 'tags\\['"),
    ],
)
def test_expansion_errors(records: list[WorldRecord] | None, vary: list[str], message: str) -> None:
    spec = ExpansionSpec(target_count=5, vary=vary)
    with pytest.raises(WorldError, match=message):
        expand_collection(PETS, _pets() if records is None else records, spec)


def test_int_keys_spelled_as_strings_keep_their_spelling() -> None:
    originals = [WorldRecord(key=7, record={"id": "7", "name": "nemo"})]
    expanded = expand_collection(PETS, originals, ExpansionSpec(target_count=3))
    assert [record.key for record in expanded] == [7, 8, 9]
    assert [_payload(record)["id"] for record in expanded] == ["7", "8", "9"]
    assert expanded[1].source == "expansion:7"  # no recorded source: the template's key
    assert expanded[1].recorded_at is None


def test_uuid_keys_are_unique_seeded_uuid4s() -> None:
    originals = records_from_values(
        UPLOADS,
        [
            {"upload_id": REPORT_ID.upper(), "filename": "q3-report.pdf", "status": "complete"},
            {"upload_id": SCAN_ID, "filename": "scan.png", "status": "processing"},
        ],
    )
    spec = ExpansionSpec(target_count=12, seed=3, vary=["status"])
    expanded = expand_collection(UPLOADS, originals, spec)
    keys = [record.key for record in expanded]
    assert keys[:2] == [REPORT_ID, SCAN_ID]
    assert len(set(keys)) == 12
    for record in expanded[2:]:
        assert isinstance(record.key, str)
        assert uuid.UUID(record.key).version == 4
        assert str(uuid.UUID(record.key)) == record.key
        assert _payload(record)["upload_id"] == record.key
        assert _payload(record)["status"] in {"complete", "processing"}
    assert expand_collection(UPLOADS, originals, spec) == expanded


def test_uuid_collisions_are_drawn_again() -> None:
    rng = random.Random(3)
    first, second = (str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(2))
    originals = records_from_values(UPLOADS, [{"upload_id": first.upper(), "filename": "a.pdf"}])
    expanded = expand_collection(UPLOADS, originals, ExpansionSpec(target_count=2, seed=3))
    assert [record.key for record in expanded] == [first, second]


def test_ticker_like_codes_get_new_codes_of_the_same_length() -> None:
    originals = records_from_values(TICKERS, TICKER_VALUES)
    expanded = expand_collection(TICKERS, originals, ExpansionSpec(target_count=30, seed=1))
    keys = [record.key for record in expanded]
    assert len(set(keys)) == 30
    for template, record in zip(itertools.cycle(originals), expanded[3:], strict=False):
        assert isinstance(record.key, str)
        assert isinstance(template.key, str)
        assert re.fullmatch(f"[A-Z]{{{len(template.key)}}}", record.key)
        assert _payload(record)["symbol"] == record.key


def test_short_codes_are_used_up_before_failing() -> None:
    codes = CollectionModel(name="states", key="code", key_type="str")
    originals = records_from_values(codes, [{"code": "AA", "name": "Alpha"}])
    every_code = {"".join(pair) for pair in itertools.product(string.ascii_uppercase, repeat=2)}
    expanded = expand_collection(codes, originals, ExpansionSpec(target_count=676))
    assert {record.key for record in expanded} == every_code
    with pytest.raises(WorldError, match="collection 'states': every 2-letter code is taken"):
        expand_collection(codes, originals, ExpansionSpec(target_count=677))


def test_other_string_keys_get_numbered_suffixes() -> None:
    documents = CollectionModel(name="documents", key="slug", key_type="str")
    originals = records_from_values(
        documents, [{"slug": "doc-a", "title": "A"}, {"slug": "doc-a-1", "title": "A, again"}]
    )
    expanded = expand_collection(documents, originals, ExpansionSpec(target_count=5))
    assert [record.key for record in expanded] == [
        "doc-a",
        "doc-a-1",
        "doc-a-2",
        "doc-a-1-1",
        "doc-a-3",
    ]
    mixed = records_from_values(TICKERS, [{"symbol": "AAPL"}, {"symbol": "BRK.B"}])
    expanded = expand_collection(TICKERS, mixed, ExpansionSpec(target_count=4))
    assert [record.key for record in expanded[2:]] == ["AAPL-1", "BRK.B-1"]


def test_expansion_keeps_relations_valid(tmp_path: Path) -> None:
    exchanges = CollectionModel(name="exchanges", key="code", key_type="str")
    relation = RelationModel(
        name="ticker_exchange",
        from_collection="tickers",
        from_field="exchange",
        to_collection="exchanges",
    )
    model = ApiModel(
        name="markets",
        title="Markets API",
        source=SourceInfo(kind="openapi", url="https://api.example.test/spec", fetched_at=NOW),
        real=RealTarget(base_url="https://api.example.test"),
        collections=[exchanges, TICKERS],
        relations=[relation],
    )
    records = {
        "exchanges": records_from_values(
            exchanges, [{"code": code} for code in ("XNAS", "XNYS", "XLON")]
        ),
        "tickers": records_from_values(TICKERS, TICKER_VALUES, recorded_at=NOW),
    }
    session = World.from_records(model, records).session()
    targets = resolve_relation_targets(model, session)
    reference = {relation.from_field: targets[relation.name]}

    spec = ExpansionSpec(target_count=60, seed=11, vary=["exchange", "price"])
    tickers = expand_collection(TICKERS, records["tickers"], spec, reference_values=reference)
    exchanges_used = {_payload(record)["exchange"] for record in tickers[3:]}
    assert exchanges_used == {"XNAS", "XNYS", "XLON"}  # XLON was never observed on a ticker
    assert {_payload(record)["price"] for record in tickers[3:]} <= {105.5, 187.0, 411.2}

    only_london = expand_collection(
        TICKERS, records["tickers"], spec, reference_values={"$.exchange": ["XLON"]}
    )
    assert {_payload(record)["exchange"] for record in only_london[3:]} == {"XLON"}

    expanded_world = World.from_records(model, {**records, "tickers": tickers})
    assert check_world(model, expanded_world.session()) == []

    path = tmp_path / TICKERS.path
    save_collection(path, tickers)
    assert load_collection(path) == tickers
    assert '"price":105.50' in path.read_text()
    assert '"synthetic":true' in path.read_text()
