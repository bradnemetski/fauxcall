from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fauxcall.errors import WorldError
from fauxcall.jsonfmt import JsonFloat, dumps
from fauxcall.model import ApiModel, CollectionModel, RealTarget, SourceInfo, WorldRecord
from fauxcall.storage import read_json, write_json
from fauxcall.world import (
    DuplicateKeyError,
    InvalidKeyError,
    SessionWorld,
    World,
    load_collection,
    records_from_values,
    save_collection,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fauxcall.types import JsonObject, JsonValue

NOW = datetime(2026, 10, 10, 14, 30, tzinfo=UTC)
LATER = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)

REPORT_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
SCAN_ID = "9b2e4c1a-0d6f-4e8b-a1c3-5f7e9d2b4a60"
NEW_ID = "c56a4180-65aa-42ec-a945-5fd21dec0538"

EXCHANGES: list[JsonValue] = [
    {"code": "XNAS", "name": "Nasdaq", "country": "US"},
    {"code": "XNYS", "name": "New York Stock Exchange", "country": "US"},
]
TICKERS: list[JsonValue] = [
    {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "XNAS", "price": JsonFloat("105.50")},
    {"symbol": "MSFT", "name": "Microsoft", "exchange": "XNAS", "price": JsonFloat("411.20")},
    {"symbol": "IBM", "name": "IBM", "exchange": "XNYS", "price": JsonFloat("187.00")},
]
PETS: list[JsonValue] = [  # recorded out of key order; the collection lists by key
    {"id": 3, "name": "rex", "tags": [{"id": 2, "name": "guard"}], "status": "sold"},
    {"id": 1, "name": "doggie", "tags": [{"id": 1, "name": "friendly"}], "status": "available"},
    {"id": 2, "name": "kitty", "tags": [], "status": "pending"},
]
UPLOADS: list[JsonValue] = [
    {"upload_id": REPORT_ID.upper(), "filename": "q3-report.pdf", "status": "complete"},
    {"upload_id": SCAN_ID, "filename": "scan.png", "status": "processing"},
]


def _model() -> ApiModel:
    return ApiModel(
        name="markets",
        title="Markets API",
        source=SourceInfo(kind="openapi", url="https://api.example.test/spec", fetched_at=NOW),
        real=RealTarget(base_url="https://api.example.test"),
        collections=[
            CollectionModel(name="exchanges", key="code", key_type="str"),
            CollectionModel(name="tickers", key="symbol", key_type="str"),
            CollectionModel(name="pets", order="key_asc"),
            CollectionModel(name="uploads", key="upload_id", key_type="uuid", order="key_desc"),
            CollectionModel(name="breeds", key="$", key_type="str"),
            CollectionModel(name="orders"),
        ],
    )


def _records(model: ApiModel) -> dict[str, list[WorldRecord]]:
    values = {"exchanges": EXCHANGES, "tickers": TICKERS, "pets": PETS, "uploads": UPLOADS}
    return {
        name: records_from_values(model.collection(name), items, recorded_at=NOW, source=name)
        for name, items in values.items()
    } | {"breeds": [WorldRecord(key="akita", record="akita")]}


@pytest.fixture
def world() -> World:
    model = _model()
    return World.from_records(model, _records(model))


def _keys(session: SessionWorld, collection: str) -> list[JsonValue]:
    return [record.key for record in session.list(collection)]


def _field(record: WorldRecord | None, name: str) -> JsonValue:
    assert record is not None
    assert isinstance(record.record, dict)
    return record.record[name]


# ---------------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------------


def test_world_loads_collection_files(tmp_path: Path) -> None:
    model = _model()
    for name, records in _records(model).items():
        save_collection(tmp_path / model.collection(name).path, records)
    (tmp_path / "world" / "breeds.jsonl").unlink()
    world = World(model, tmp_path)
    session = world.session()
    assert session.collections() == ["exchanges", "tickers", "pets", "uploads", "breeds", "orders"]
    assert world.model is model
    assert _keys(session, "tickers") == ["AAPL", "MSFT", "IBM"]
    assert session.count("breeds") == 0
    assert session.count("orders") == 0
    price = _field(session.get("tickers", "AAPL"), "price")
    assert isinstance(price, JsonFloat)
    assert price.lexeme == "105.50"


def test_world_reports_invalid_files(tmp_path: Path) -> None:
    path = tmp_path / "world" / "pets.jsonl"
    path.parent.mkdir()
    path.write_text('{"key":1,"record":{"id":1}}\n{"key":2,"record":\n')
    with pytest.raises(WorldError, match=r"pets\.jsonl:2: invalid world record"):
        World(_model(), tmp_path)


def test_base_keys_are_normalized_and_checked() -> None:
    model = _model()
    pets = [WorldRecord(key="12", record={"id": 12}), WorldRecord(key=13, record={"name": "x"})]
    world = World.from_records(model, {"pets": pets})
    assert world.session().keys("pets") == [12, 13]
    assert [record.key for record in world.base_records("pets")] == [12, 13]
    assert world.base_records("pets")[1].record == {"name": "x"}  # a missing key field is fine

    with pytest.raises(DuplicateKeyError, match="collection 'pets': duplicate key 12"):
        World.from_records(model, {"pets": [*pets, WorldRecord(key=12.0, record={})]})
    with pytest.raises(WorldError, match=r"collection 'pets': record key 1 does not match id = 2"):
        World.from_records(model, {"pets": [WorldRecord(key=1, record={"id": "2"})]})
    with pytest.raises(InvalidKeyError, match="collection 'pets': 'abc' is not a valid int key"):
        World.from_records(model, {"pets": [WorldRecord(key="abc", record={})]})
    with pytest.raises(WorldError, match="records given for unknown collections: owners"):
        World.from_records(model, {"owners": []})


def test_base_records_are_copies_detached_from_inputs() -> None:
    model = _model()
    records = _records(model)
    world = World.from_records(model, records)
    first = records["pets"][0].record
    assert isinstance(first, dict)
    first["name"] = "mutated input"
    base = world.base_records("pets")
    assert [record.key for record in base] == [1, 2, 3]
    assert _field(base[2], "name") == "rex"
    base[2].record = {"id": 3, "name": "mutated output"}
    assert _field(world.session().get("pets", 3), "name") == "rex"
    with pytest.raises(WorldError, match="unknown collection 'owners'"):
        world.base_records("owners")


# ---------------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------------


def test_keys_are_normalized_for_lookups(world: World) -> None:
    session = world.session()
    assert session.get("pets", "2") == session.get("pets", 2)
    assert _field(session.get("pets", "002"), "name") == "kitty"
    assert _field(session.get("pets", 2.0), "name") == "kitty"
    report = session.get("uploads", REPORT_ID.upper())
    assert report is not None
    assert report.key == REPORT_ID
    assert _field(report, "upload_id") == REPORT_ID.upper()  # the payload keeps its spelling
    assert session.get("pets", 99) is None
    assert session.get("tickers", "aapl") is None
    with pytest.raises(InvalidKeyError, match="collection 'pets': 'abc' is not a valid int key"):
        session.get("pets", "abc")
    with pytest.raises(InvalidKeyError, match="not a valid uuid key"):
        session.get("uploads", "not-a-uuid")


def test_ordering_modes(world: World) -> None:
    session = world.session()
    assert _keys(session, "tickers") == ["AAPL", "MSFT", "IBM"]  # recorded
    assert _keys(session, "pets") == [1, 2, 3]  # key_asc
    assert _keys(session, "uploads") == [SCAN_ID, REPORT_ID]  # key_desc
    assert session.keys("pets") == [1, 2, 3]

    session.insert("tickers", {"symbol": "AMZN", "exchange": "XNAS"})
    session.update("tickers", "AAPL", {"price": JsonFloat("106.00")})
    session.insert("pets", {"id": 0, "name": "puppy", "tags": []})
    session.insert("pets", {"id": 10, "name": "old timer", "tags": []})
    session.insert("uploads", {"upload_id": NEW_ID, "filename": "new.csv", "status": "queued"})
    assert _keys(session, "tickers") == ["AAPL", "MSFT", "IBM", "AMZN"]
    assert _keys(session, "pets") == [0, 1, 2, 3, 10]
    assert _keys(session, "uploads") == [NEW_ID, SCAN_ID, REPORT_ID]
    assert session.keys("pets") == [0, 1, 2, 3, 10]


def test_returned_records_are_copies(world: World) -> None:
    session = world.session()
    record = session.get("pets", 1)
    assert record is not None
    assert isinstance(record.record, dict)
    record.record["name"] = "mutated"
    listed = session.list("pets")[0]
    assert isinstance(listed.record, dict)
    listed.record["status"] = "mutated"
    assert session.get("pets", 1) == world.base_records("pets")[0]
    assert not session.is_modified()

    views = session.list("pets", copy=False)
    assert all(a is b for a, b in zip(views, session.list("pets", copy=False), strict=True))
    assert views == session.list("pets")


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get("owners", 1),
        lambda s: s.list("owners"),
        lambda s: s.keys("owners"),
        lambda s: s.count("owners"),
        lambda s: s.collection("owners"),
        lambda s: s.insert("owners", {"id": 1}),
        lambda s: s.replace("owners", 1, {"id": 1}),
        lambda s: s.update("owners", 1, {}),
        lambda s: s.upsert("owners", {"id": 1}),
        lambda s: s.delete("owners", 1),
        lambda s: s.next_int_key("owners"),
    ],
)
def test_unknown_collections_are_rejected(
    world: World, call: Callable[[SessionWorld], object]
) -> None:
    with pytest.raises(WorldError, match=r"unknown collection 'owners' \(known: 'exchanges', "):
        call(world.session())


def test_unknown_collection_message_without_collections() -> None:
    world = World.from_records(_model().model_copy(update={"collections": []}), {})
    with pytest.raises(WorldError, match=r"unknown collection 'pets' \(known: none\)"):
        world.session().count("pets")


# ---------------------------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------------------------


def test_sessions_are_isolated(world: World) -> None:
    eval_a = world.session("eval-a")
    eval_b = world.session("eval-b")
    assert world.session("eval-a") is eval_a
    assert world.sessions() == ["eval-a", "eval-b"]

    eval_a.insert("tickers", {"symbol": "AMZN", "exchange": "XNAS"})
    eval_a.update("tickers", "AAPL", {"price": JsonFloat("1.00")})
    eval_a.delete("tickers", "IBM")
    assert _keys(eval_a, "tickers") == ["AAPL", "MSFT", "AMZN"]
    assert _keys(eval_b, "tickers") == ["AAPL", "MSFT", "IBM"]
    assert eval_b.get("tickers", "AMZN") is None
    assert _field(eval_b.get("tickers", "AAPL"), "price") == JsonFloat("105.50")
    assert [r.key for r in world.base_records("tickers")] == ["AAPL", "MSFT", "IBM"]
    assert eval_a.is_modified()
    assert not eval_b.is_modified()
    assert eval_a.session_id == "eval-a"


def test_insert(world: World) -> None:
    session = world.session()
    payload: JsonObject = {"id": "4", "name": "goldie", "tags": [{"id": 1, "name": "friendly"}]}
    inserted = session.insert("pets", payload, recorded_at=LATER, source="POST /pet")
    assert inserted.key == 4
    assert inserted.recorded_at == LATER
    assert inserted.source == "POST /pet"
    assert not inserted.synthetic
    payload["name"] = "mutated after insert"
    assert _field(session.get("pets", 4), "name") == "goldie"
    assert session.count("pets") == 4

    with pytest.raises(DuplicateKeyError, match="collection 'pets': a record with key 4 already"):
        session.insert("pets", {"id": 4})
    with pytest.raises(DuplicateKeyError, match="key 1 already exists"):
        session.insert("pets", {"id": 1})
    with pytest.raises(WorldError, match="record has no key at 'id'"):
        session.insert("pets", {"name": "nameless"})


def test_replace(world: World) -> None:
    session = world.session()
    assert session.replace("pets", 99, {"name": "ghost"}) is None
    replaced = session.replace("pets", "2", {"name": "kitty", "status": "sold"})
    assert replaced is not None
    assert replaced.record == {"name": "kitty", "status": "sold", "id": 2}
    assert replaced.recorded_at == NOW  # metadata of the replaced record is kept
    assert replaced.source == "pets"
    assert session.get("pets", 2) == replaced
    assert _keys(session, "pets") == [1, 2, 3]
    same_key = session.replace("pets", 2, {"id": "2", "name": "kitty"})
    assert same_key is not None
    assert same_key.record == {"id": "2", "name": "kitty"}
    with pytest.raises(WorldError, match="cannot change the key of record 2 to 3"):
        session.replace("pets", 2, {"id": 3, "name": "impostor"})
    with pytest.raises(WorldError, match="cannot change the key of record 'akita' to 'husky'"):
        session.replace("breeds", "akita", "husky")  # type: ignore[arg-type]  # record is the key


def test_update(world: World) -> None:
    session = world.session()
    assert session.update("pets", 99, {"status": "sold"}) is None
    updated = session.update("pets", 1, {"status": "sold", "tags": [{"id": 2, "name": "guard"}]})
    assert updated is not None
    assert updated.record == {
        "id": 1,
        "name": "doggie",
        "tags": [{"id": 2, "name": "guard"}],
        "status": "sold",
    }
    assert session.update("pets", "1", {"id": "1"}) is not None  # same key, other spelling
    assert session.update("pets", 1, {"id": None}) is not None  # the key is put back
    assert _field(session.get("pets", 1), "id") == 1
    with pytest.raises(WorldError, match="cannot change the key of record 1 to 5"):
        session.update("pets", 1, {"id": 5})
    with pytest.raises(WorldError, match="record 'akita' is not an object"):
        session.update("breeds", "akita", {"size": "large"})
    assert world.base_records("pets")[0].record == PETS[1]


def test_upsert(world: World) -> None:
    session = world.session()
    inserted = session.upsert("tickers", {"symbol": "AMZN", "exchange": "XNAS"}, source="PUT")
    assert inserted.source == "PUT"
    replaced = session.upsert("tickers", {"symbol": "AAPL", "exchange": "XNYS"}, source="PUT")
    assert replaced.source == "tickers"  # replacing keeps the recorded metadata
    assert replaced.record == {"symbol": "AAPL", "exchange": "XNYS"}
    assert _keys(session, "tickers") == ["AAPL", "MSFT", "IBM", "AMZN"]


def test_delete_and_reinsert(world: World) -> None:
    session = world.session()
    assert session.delete("tickers", "MSFT")
    assert not session.delete("tickers", "MSFT")
    assert not session.delete("tickers", "NOPE")
    assert session.get("tickers", "MSFT") is None
    assert session.count("tickers") == 2
    assert _keys(session, "tickers") == ["AAPL", "IBM"]

    # Re-creating a deleted base record lists it like any new record (at the end).
    session.insert("tickers", {"symbol": "MSFT", "exchange": "XNYS"})
    assert _keys(session, "tickers") == ["AAPL", "IBM", "MSFT"]
    assert _field(session.get("tickers", "MSFT"), "exchange") == "XNYS"
    assert session.count("tickers") == 3

    session.insert("tickers", {"symbol": "AMZN", "exchange": "XNAS"})
    assert session.delete("tickers", "AMZN")
    assert session.delete("tickers", "MSFT")
    assert _keys(session, "tickers") == ["AAPL", "IBM"]
    assert session.count("tickers") == 2
    session.insert("tickers", {"symbol": "AMZN", "exchange": "XNAS"})
    assert session.count("tickers") == 3
    with pytest.raises(InvalidKeyError):
        session.delete("pets", "abc")


def test_next_int_key_never_reuses_keys(world: World) -> None:
    session = world.session()
    assert session.next_int_key("pets") == 4
    assert session.next_int_key("pets") == 5  # every call consumes a key
    session.insert("pets", {"id": 6, "name": "goldie"})
    assert session.next_int_key("pets") == 7
    session.insert("pets", {"id": 8, "name": "nemo"})
    session.delete("pets", 8)
    session.delete("pets", 3)
    assert session.next_int_key("pets") == 9
    assert session.next_int_key("pets", start=1000) == 1000
    assert session.next_int_key("pets", start=10) == 1001
    assert session.next_int_key("orders") == 1  # empty collection
    assert session.next_int_key("orders", start=500) == 500
    with pytest.raises(WorldError, match="collection 'tickers' has str keys, not int keys"):
        session.next_int_key("tickers")
    other = world.session("other")
    assert other.next_int_key("pets") == 4
    world.reset()
    assert session.next_int_key("pets") == 4


def test_key_reservations_count_as_modifications(world: World) -> None:
    session = world.session()
    session.insert("pets", {"id": 4, "name": "goldie"})
    session.delete("pets", 4)
    assert session.list("pets") == world.base_records("pets")
    assert session.is_modified()  # the data is back to the seed, but key 4 was used
    assert session.next_int_key("pets") == 5


# ---------------------------------------------------------------------------------------------
# Reset, snapshot, restore
# ---------------------------------------------------------------------------------------------


def _scenario(session: SessionWorld) -> None:
    session.insert("tickers", {"symbol": "AMZN", "exchange": "XNAS", "price": JsonFloat("12.50")})
    session.update("tickers", "AAPL", {"price": JsonFloat("106.00")})
    session.delete("tickers", "IBM")
    session.insert("pets", {"id": 7, "name": "goldie", "tags": []}, recorded_at=LATER)
    session.update("pets", 3, {"status": "available"})
    session.delete("pets", 1)
    session.insert("pets", {"id": 1, "name": "doggie 2", "tags": []})
    session.next_int_key("pets", start=100)
    session.delete("uploads", SCAN_ID)


def _view(session: SessionWorld) -> dict[str, list[WorldRecord]]:
    return {name: session.list(name) for name in session.collections()}


def test_reset_returns_to_the_seed(world: World) -> None:
    session = world.session("eval")
    seed = _view(session)
    _scenario(session)
    assert _view(session) != seed
    world.reset("eval")
    assert world.session("eval") is session
    assert _view(session) == seed
    assert not session.is_modified()

    _scenario(session)
    _scenario(world.session("other"))
    world.reset("unknown")  # nothing to reset
    assert session.is_modified()
    world.reset()
    assert not session.is_modified()
    assert not world.session("other").is_modified()
    session.insert("pets", {"id": 5})
    session.reset()
    assert _view(session) == seed


def test_snapshot_and_restore(world: World) -> None:
    session = world.session("eval")
    assert session.snapshot() == {"format_version": 1}
    _scenario(session)
    snapshot = session.snapshot()
    assert json.loads(dumps(snapshot)) == {
        "format_version": 1,
        "collections": {
            "pets": {
                "changed": [
                    {
                        "key": 3,
                        "record": {
                            "id": 3,
                            "name": "rex",
                            "tags": [{"id": 2, "name": "guard"}],
                            "status": "available",
                        },
                        "recorded_at": "2026-10-10T14:30:00Z",
                        "source": "pets",
                    }
                ],
                "added": [
                    {
                        "key": 7,
                        "record": {"id": 7, "name": "goldie", "tags": []},
                        "recorded_at": "2026-10-11T09:00:00Z",
                    },
                    {"key": 1, "record": {"id": 1, "name": "doggie 2", "tags": []}},
                ],
                "deleted": [1],
                "last_key": 100,
            },
            "tickers": {
                "changed": [
                    {
                        "key": "AAPL",
                        "record": {
                            "symbol": "AAPL",
                            "name": "Apple Inc.",
                            "exchange": "XNAS",
                            "price": 106.0,
                        },
                        "recorded_at": "2026-10-10T14:30:00Z",
                        "source": "tickers",
                    }
                ],
                "added": [
                    {"key": "AMZN", "record": {"symbol": "AMZN", "exchange": "XNAS", "price": 12.5}}
                ],
                "deleted": ["IBM"],
            },
            "uploads": {"deleted": [SCAN_ID]},
        },
    }
    assert '"price":106.00' in dumps(snapshot)  # float spellings survive
    assert '"price":12.50' in dumps(snapshot)

    clone = world.session("clone")
    clone.restore(snapshot)
    assert _view(clone) == _view(session)
    assert clone.snapshot() == snapshot
    assert clone.count("pets") == session.count("pets") == 4
    assert clone.next_int_key("pets") == session.next_int_key("pets") == 101

    session.update("tickers", "MSFT", {"price": JsonFloat("1.00")})
    assert clone.get("tickers", "MSFT") == world.base_records("tickers")[1]  # independent
    session.restore(snapshot)
    assert _view(session) == _view(clone)
    session.restore({"format_version": 1})
    assert not session.is_modified()


def test_snapshots_are_detached(world: World) -> None:
    session = world.session()
    session.insert("pets", {"id": 9, "name": "goldie", "tags": []})
    snapshot = session.snapshot()
    session.update("pets", 9, {"name": "changed later"})
    assert isinstance(snapshot, dict)
    assert "goldie" in dumps(snapshot)
    restored = world.session("restored")
    restored.restore(snapshot)
    snapshot.clear()
    assert _field(restored.get("pets", 9), "name") == "goldie"


@pytest.mark.parametrize(
    ("snapshot", "message"),
    [
        ({}, "invalid world snapshot"),
        ({"format_version": 2}, "invalid world snapshot"),
        ([], "invalid world snapshot"),
        ({"format_version": 1, "collections": {"owners": {}}}, "unknown collection 'owners'"),
        (
            {"format_version": 1, "collections": {"pets": {"added": [{"key": "x", "record": {}}]}}},
            "'x' is not a valid int key",
        ),
        (
            {
                "format_version": 1,
                "collections": {"pets": {"added": [{"key": 4, "record": {"id": 5}}]}},
            },
            "record key 4 does not match id = 5",
        ),
        (
            {"format_version": 1, "collections": {"pets": {"deleted": ["abc"]}}},
            "'abc' is not a valid int key",
        ),
    ],
)
def test_restore_rejects_invalid_snapshots(world: World, snapshot: JsonValue, message: str) -> None:
    session = world.session()
    _scenario(session)
    before = session.snapshot()
    with pytest.raises(WorldError, match=message):
        session.restore(snapshot)
    assert session.snapshot() == before  # all or nothing


def test_restore_tolerates_a_changed_base(world: World) -> None:
    stale: JsonValue = {
        "format_version": 1,
        "collections": {
            "pets": {
                "changed": [
                    {"key": 2, "record": {"id": 2, "name": "kitty", "status": "sold"}},
                    {"key": 42, "record": {"id": 42, "name": "gone from the base"}},
                    {"key": 3, "record": {"id": 3, "name": "deleted and changed"}},
                ],
                "added": [{"key": 1, "record": {"id": 1, "name": "now also in the base"}}],
                "deleted": [3, 77],
            }
        },
    }
    session = world.session()
    session.restore(stale)
    assert session.keys("pets") == [1, 2, 3, 42]
    assert _field(session.get("pets", 1), "name") == "now also in the base"
    assert _field(session.get("pets", 2), "status") == "sold"
    assert _field(session.get("pets", 3), "name") == "deleted and changed"
    assert _field(session.get("pets", 42), "name") == "gone from the base"
    assert session.count("pets") == 4
    assert session.next_int_key("pets") == 43
    snapshot = session.snapshot()
    assert isinstance(snapshot, dict)
    assert json.loads(dumps(snapshot))["collections"]["pets"]["deleted"] == [1, 3]


# ---------------------------------------------------------------------------------------------
# Sessions and persistence
# ---------------------------------------------------------------------------------------------


def test_drop_forgets_a_session(world: World) -> None:
    session = world.session("eval")
    _scenario(session)
    assert world.drop("eval")
    assert not world.drop("eval")
    assert world.sessions() == []
    assert not world.session("eval").is_modified()


def test_save_and_load_state(world: World, tmp_path: Path) -> None:
    path = tmp_path / "state" / "world.json"
    _scenario(world.session("eval-a"))
    world.session("eval-b").update("tickers", "MSFT", {"price": JsonFloat("400.10")})
    world.session("idle")
    world.save_state(path)
    text = path.read_text()
    assert '"price": 106.00' in text
    assert '"price": 400.10' in text
    state = read_json(path)
    assert isinstance(state, dict)
    assert list(state) == ["format_version", "sessions"]
    assert isinstance(state["sessions"], dict)
    assert list(state["sessions"]) == ["eval-a", "eval-b", "idle"]

    model = world.model
    restarted = World.from_records(model, _records(model))
    stale = restarted.session("stale")
    stale.insert("pets", {"id": 50})
    held = restarted.session("eval-a")
    restarted.load_state(path)
    assert restarted.sessions() == ["eval-a", "eval-b", "idle", "stale"]
    assert restarted.session("eval-a") is held
    for session_id in ("eval-a", "eval-b", "idle"):
        assert _view(restarted.session(session_id)) == _view(world.session(session_id))
    assert not stale.is_modified()
    price = _field(restarted.session("eval-b").get("tickers", "MSFT"), "price")
    assert isinstance(price, JsonFloat)
    assert price.lexeme == "400.10"


def test_load_state_is_all_or_nothing(world: World, tmp_path: Path) -> None:
    session = world.session("eval")
    _scenario(session)
    before = session.snapshot()
    path = tmp_path / "state.json"
    write_json(
        path,
        {
            "format_version": 1,
            "sessions": {
                "eval": {"format_version": 1},
                "zzz": {"format_version": 1, "collections": {"owners": {}}},
            },
        },
    )
    with pytest.raises(WorldError, match="unknown collection 'owners'"):
        world.load_state(path)
    assert session.snapshot() == before
    assert world.sessions() == ["eval"]


@pytest.mark.parametrize(
    "content", [None, "{not json", '{"format_version": 1, "sessions": []}', '{"sessions": {}}']
)
def test_load_state_rejects_missing_or_invalid_files(
    world: World, tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "state.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(WorldError, match="cannot load world state from"):
        world.load_state(path)


def test_saved_collections_reload_into_an_equal_world(world: World, tmp_path: Path) -> None:
    session = world.session()
    _scenario(session)
    for name in session.collections():
        save_collection(tmp_path / world.model.collection(name).path, session.list(name))
    assert load_collection(tmp_path / "world" / "tickers.jsonl")[0].record == {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "exchange": "XNAS",
        "price": JsonFloat("106.00"),
    }
    assert '"price":106.00' in (tmp_path / "world" / "tickers.jsonl").read_text()
    frozen = World(world.model, tmp_path)
    assert _view(frozen.session()) == _view(session)
