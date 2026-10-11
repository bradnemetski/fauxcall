"""``fauxcall.build.world``: collections built from recordings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from fauxcall.build.world import (
    CollectionBuild,
    build_collection,
    build_world,
    summary,
    write_world,
)
from fauxcall.errors import WorldError
from fauxcall.model import (
    ApiModel,
    Body,
    CapturedRequest,
    CapturedResponse,
    CollectionModel,
    Exchange,
    RealTarget,
    SourceInfo,
)
from fauxcall.types import HttpMethod
from fauxcall.world.io import load_collection

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.model.common import Purpose
    from fauxcall.types import JsonValue

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
LIST = "GET /pets"
ONE = "GET /pets/{id}"


def exchange(
    exchange_id: str,
    endpoint: str,
    body: JsonValue,
    *,
    status: int = 200,
    purpose: Purpose = "sample",
    minutes: int = 0,
    text: bool = False,
    answered: bool = True,
) -> Exchange:
    at = T0 + timedelta(minutes=minutes)
    content = Body(kind="text", text=str(body)) if text else Body(kind="json", json_value=body)
    return Exchange(
        id=exchange_id,
        endpoint=endpoint,
        purpose=purpose,
        started_at=at,
        started_ns=int(at.timestamp()) * 1_000_000_000,
        request=CapturedRequest(method=HttpMethod.GET, url="https://api.test/pets", path="/pets"),
        response=CapturedResponse(status=status, body=content) if answered else None,
    )


def pets(**fields: object) -> CollectionModel:
    return CollectionModel.model_validate({"name": "pets", **fields})


def keys(build: CollectionBuild) -> list[JsonValue]:
    return [record.key for record in build.records]


def test_extract_paths_are_honored() -> None:
    collection = pets(extract={LIST: "$.data[*]", ONE: "$"})
    build = build_collection(
        collection,
        [
            exchange("1", LIST, {"data": [{"id": 1}, {"id": 2}], "total": 2}),
            exchange("2", ONE, {"id": 3, "name": "Rex"}, minutes=1),
            exchange("3", "GET /owners", {"id": 4}, minutes=2),
        ],
    )
    assert build.name == "pets"
    assert keys(build) == [1, 2, 3]
    assert build.records[2].record == {"id": 3, "name": "Rex"}
    assert build.records[2].source == "2"
    assert build.records[2].recorded_at == T0 + timedelta(minutes=1)
    assert build.exchanges_used == 2


def test_sources_without_an_extract_path_use_the_whole_body() -> None:
    build = build_collection(pets(sources=[ONE]), [exchange("1", ONE, {"id": 7})])
    assert keys(build) == [7]


def test_an_array_at_the_extraction_point_contributes_its_items() -> None:
    collection = pets(sources=[LIST], extract={"GET /search": "$.items"})
    build = build_collection(
        collection,
        [
            exchange("1", LIST, [{"id": 1}, {"id": 2}]),
            exchange("2", "GET /search", {"items": [{"id": 3}, [{"id": 99}]]}, minutes=1),
        ],
    )
    assert keys(build) == [1, 2, 3]


def test_only_allowed_purposes_feed_the_world() -> None:
    recorded = [
        exchange("1", ONE, {"id": 1}),
        exchange("2", ONE, {"id": 2}, purpose="write_readback", minutes=1),
        exchange("3", ONE, {"id": 3}, purpose="invalid", minutes=2),
        exchange("4", ONE, {"id": 4}, purpose="pagination", minutes=3),
    ]
    assert keys(build_collection(pets(sources=[ONE]), recorded)) == [1, 4]
    widened = pets(sources=[ONE], purposes=["sample", "write_readback"])
    assert keys(build_collection(widened, recorded)) == [1, 2]


def test_only_successful_json_responses_count() -> None:
    build = build_collection(
        pets(sources=[ONE]),
        [
            exchange("1", ONE, {"id": 1}, status=404),
            exchange("2", ONE, {"id": 2}, status=500, minutes=1),
            exchange("3", ONE, {"id": 3}, status=302, minutes=2),
            exchange("4", ONE, {"id": 4}, answered=False, minutes=3),
            exchange("5", ONE, "id=5", text=True, minutes=4),
            exchange("6", ONE, {"id": 6}, status=201, minutes=5),
            exchange("7", ONE, {"id": 7}, status=299, minutes=6),
        ],
    )
    assert keys(build) == [6, 7]
    assert build.exchanges_used == 2


def test_the_freshest_record_wins_but_keeps_its_first_position() -> None:
    build = build_collection(
        pets(sources=[LIST]),
        [
            exchange("3", LIST, [{"id": 2, "name": "newest"}, {"id": 3}], minutes=10),
            exchange("1", LIST, [{"id": 1, "name": "a"}, {"id": 2, "name": "old"}]),
            exchange("2", LIST, [{"id": 2, "name": "newer"}], minutes=5),
        ],
    )
    assert keys(build) == [1, 2, 3]
    second = build.records[1]
    assert second.record == {"id": 2, "name": "newest"}
    assert second.source == "3"
    assert second.recorded_at == T0 + timedelta(minutes=10)
    assert build.replaced == 2
    assert build.exchanges_used == 3


def test_simultaneous_recordings_are_ordered_by_id() -> None:
    build = build_collection(
        pets(sources=[ONE]),
        [exchange("b", ONE, {"id": 1, "v": "b"}), exchange("a", ONE, {"id": 1, "v": "a"})],
    )
    assert build.records[0].record == {"id": 1, "v": "b"}
    assert build.replaced == 1


def test_records_without_a_usable_key_are_counted() -> None:
    build = build_collection(
        pets(sources=[LIST]),
        [
            exchange(
                "1",
                LIST,
                [{"name": "no id"}, {"id": None}, {"id": "abc"}, {"id": True}, {"id": "12"}],
            )
        ],
    )
    assert keys(build) == [12]
    assert build.skipped_without_key == 4


def test_scalars_are_not_records_of_object_collections() -> None:
    build = build_collection(pets(sources=[LIST]), [exchange("1", LIST, [1, "x", None, {"id": 4}])])
    assert keys(build) == [4]
    assert build.skipped_without_key == 0


def test_root_keyed_collections_hold_scalars() -> None:
    currencies = CollectionModel(name="currencies", key="$", key_type="str", sources=[LIST])
    build = build_collection(
        currencies, [exchange("1", LIST, ["EUR", "USD", None, ["GBP"], {"code": "JPY"}])]
    )
    assert keys(build) == ["EUR", "USD"]
    assert [record.record for record in build.records] == ["EUR", "USD"]
    assert build.skipped_without_key == 1  # the object cannot be its own key


def test_an_invalid_extract_path_is_a_world_error() -> None:
    with pytest.raises(WorldError, match=r"'pets'.*invalid extract path"):
        build_collection(pets(extract={LIST: "$["}), [exchange("1", LIST, [{"id": 1}])])


def model(*collections: CollectionModel) -> ApiModel:
    return ApiModel(
        name="shop",
        title="Shop",
        source=SourceInfo(kind="openapi", url="https://api.test/openapi.json", fetched_at=T0),
        real=RealTarget(base_url="https://api.test"),
        collections=list(collections),
    )


def test_build_and_write_the_world(tmp_path: Path) -> None:
    owners = CollectionModel(
        name="owners", key="login", key_type="str", file="custom/owners.jsonl", sources=[LIST]
    )
    api = model(pets(sources=[LIST]), owners)
    recorded = [exchange("1", LIST, [{"id": 1, "login": "ann"}, {"id": 2, "login": "bob"}])]
    builds = build_world(api, iter(recorded))
    assert list(builds) == ["pets", "owners"]
    assert keys(builds["owners"]) == ["ann", "bob"]
    written = write_world(tmp_path, api, builds)
    assert written == [tmp_path / "world" / "pets.jsonl", tmp_path / "custom" / "owners.jsonl"]
    assert [r.key for r in load_collection(written[0])] == [1, 2]
    assert [r.record for r in load_collection(written[1])] == recorded[0].response.body.json_value  # type: ignore[union-attr]


def test_only_built_collections_are_written(tmp_path: Path) -> None:
    api = model(pets(sources=[LIST]), CollectionModel(name="owners"))
    builds = {"pets": build_collection(api.collections[0], [exchange("1", LIST, [{"id": 1}])])}
    assert write_world(tmp_path, api, builds) == [tmp_path / "world" / "pets.jsonl"]
    assert not (tmp_path / "world" / "owners.jsonl").exists()


def test_summary_table() -> None:
    build = CollectionBuild(name="pets", exchanges_used=3, skipped_without_key=1, replaced=2)
    assert summary({"pets": build}).splitlines() == [
        "| collection | records | from exchanges | replaced (fresher) | without key |",
        "|---|---|---|---|---|",
        "| pets | 0 | 3 | 2 | 1 |",
    ]
