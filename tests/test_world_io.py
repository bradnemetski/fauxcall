from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from fauxcall.errors import WorldError
from fauxcall.jsonfmt import JsonFloat
from fauxcall.model.world import CollectionModel, WorldRecord
from fauxcall.world.io import (
    _line_number,
    copy_json,
    copy_record,
    load_collection,
    records_from_values,
    save_collection,
)
from fauxcall.world.keys import InvalidKeyError

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonValue

RECORDED_AT = datetime(2026, 10, 10, 14, 30, tzinfo=UTC)
TICKERS = CollectionModel(name="tickers", key="symbol", key_type="str")
PETS = CollectionModel(name="pets")


def _price(record: WorldRecord) -> JsonFloat:
    assert isinstance(record.record, dict)
    price = record.record["price"]
    assert isinstance(price, JsonFloat)
    return price


def test_copy_json_is_deep_and_keeps_float_spellings() -> None:
    price = JsonFloat("105.50")
    original: JsonValue = {"symbol": "AAPL", "price": price, "tags": [{"id": 1}]}
    copied = copy_json(original)
    assert copied == original
    assert isinstance(copied, dict)
    assert isinstance(original, dict)
    assert copied["price"] is price
    tags = copied["tags"]
    assert isinstance(tags, list)
    assert tags is not original["tags"]
    tags.append({"id": 2})
    assert original["tags"] == [{"id": 1}]


def test_copy_record_detaches_the_payload() -> None:
    record = WorldRecord(key=1, record={"id": 1, "tags": ["a"]}, source="ex-1")
    copied = copy_record(record)
    assert copied == record
    assert isinstance(copied.record, dict)
    copied.record["tags"] = ["b"]
    assert record.record == {"id": 1, "tags": ["a"]}


def test_save_and_load_round_trip_preserves_float_spellings(tmp_path: Path) -> None:
    path = tmp_path / "world" / "tickers.jsonl"
    records = [
        WorldRecord(
            key="AAPL",
            record={"symbol": "AAPL", "exchange": "XNAS", "price": JsonFloat("105.50")},
            recorded_at=RECORDED_AT,
            source="GET /tickers#1",
        ),
        WorldRecord(key="IBM", record={"symbol": "IBM", "price": JsonFloat("187.00")}),
        WorldRecord(key="ZZZZ", record={"symbol": "ZZZZ", "price": 1.5}, synthetic=True),
    ]
    save_collection(path, records)
    assert path.read_text().splitlines() == [
        (
            '{"key":"AAPL","record":{"symbol":"AAPL","exchange":"XNAS","price":105.50},'
            '"recorded_at":"2026-10-10T14:30:00Z","source":"GET /tickers#1"}'
        ),
        '{"key":"IBM","record":{"symbol":"IBM","price":187.00}}',
        '{"key":"ZZZZ","record":{"symbol":"ZZZZ","price":1.5},"synthetic":true}',
    ]
    loaded = load_collection(path)
    assert loaded == records
    assert _price(loaded[0]).lexeme == "105.50"
    assert _price(loaded[1]).lexeme == "187.00"
    save_collection(path, loaded)
    assert "105.50" in path.read_text()
    assert "187.00" in path.read_text()


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_collection(tmp_path / "world" / "nothing.jsonl") == []


def test_load_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "pets.jsonl"
    path.write_text('\n{"key":1,"record":{"id":1}}\n\n   \n{"key":2,"record":{"id":2}}\n')
    assert [record.key for record in load_collection(path)] == [1, 2]


@pytest.mark.parametrize(
    ("third_line", "message"),
    [
        ("{not json", r"pets\.jsonl:4: invalid world record: invalid JSON \(Expecting"),
        (
            '{"key":3,"record":{"weight":NaN}}',
            r"pets\.jsonl:4: invalid world record: JSON constant 'NaN' is not valid JSON",
        ),
        ('{"key":3}', r"pets\.jsonl:4: invalid world record: record: Field required"),
        (
            '{"key":3,"record":{},"color":"red"}',
            r"pets\.jsonl:4: invalid world record: color: Extra inputs are not permitted",
        ),
        (
            '{"key":3,"record":{},"recorded_at":"2026-10-10T14:30:00"}',
            r"pets\.jsonl:4: invalid world record: recorded_at: Input should have timezone info",
        ),
        ("[1, 2]", r"pets\.jsonl:4: invalid world record: Input should be a valid dictionary"),
    ],
)
def test_load_errors_name_file_and_line(tmp_path: Path, third_line: str, message: str) -> None:
    path = tmp_path / "pets.jsonl"
    path.write_text(
        '{"key":1,"record":{"id":1}}\n\n{"key":2,"record":{"id":2}}\n' + third_line + "\n"
    )
    with pytest.raises(WorldError, match=message):
        load_collection(path)


def test_line_number_of_a_value_beyond_the_end(tmp_path: Path) -> None:
    path = tmp_path / "pets.jsonl"
    path.write_text('{"key":1,"record":{"id":1}}\n')
    assert _line_number(path, 0) == 1
    assert _line_number(path, 5) == "?"


def test_records_from_values_extracts_normalized_keys_and_copies() -> None:
    values: list[JsonValue] = [
        {"id": "7", "name": "doggie", "tags": [{"id": 1, "name": "friendly"}]},
        {"id": 3, "name": "kitty", "tags": []},
    ]
    records = records_from_values(PETS, values, recorded_at=RECORDED_AT, source="GET /pets#2")
    assert [record.key for record in records] == [7, 3]
    assert all(record.recorded_at == RECORDED_AT for record in records)
    assert all(record.source == "GET /pets#2" for record in records)
    assert not any(record.synthetic for record in records)
    first = values[0]
    assert isinstance(first, dict)
    first["name"] = "changed after recording"
    assert records[0].record == {
        "id": "7",
        "name": "doggie",
        "tags": [{"id": 1, "name": "friendly"}],
    }


def test_records_from_values_keeps_last_record_at_first_position() -> None:
    values: list[JsonValue] = [
        {"symbol": "AAPL", "price": JsonFloat("105.50")},
        {"symbol": "MSFT", "price": JsonFloat("411.20")},
        {"symbol": "AAPL", "price": JsonFloat("106.25")},
        {"symbol": "IBM", "price": JsonFloat("187.00")},
        {"symbol": "AAPL", "price": JsonFloat("107.00")},
    ]
    with capture_logs() as logs:
        records = records_from_values(TICKERS, values)
    assert [record.key for record in records] == ["AAPL", "MSFT", "IBM"]
    assert _price(records[0]).lexeme == "107.00"
    assert logs == [
        {
            "event": "duplicate world keys; keeping the last occurrence of each",
            "log_level": "warning",
            "collection": "tickers",
            "count": 1,
            "keys": ["AAPL"],
        }
    ]


def test_records_from_values_without_duplicates_logs_nothing() -> None:
    with capture_logs() as logs:
        records_from_values(TICKERS, [{"symbol": "AAPL"}])
    assert logs == []


def test_records_from_values_requires_keys() -> None:
    with pytest.raises(WorldError, match="record has no key at 'symbol'"):
        records_from_values(TICKERS, [{"symbol": "AAPL"}, {"name": "no symbol"}])
    with pytest.raises(InvalidKeyError, match="'seven' at 'id' is not a valid int key"):
        records_from_values(PETS, [{"id": "seven"}])
