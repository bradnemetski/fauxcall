from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fauxcall.errors import WorldError
from fauxcall.jsonfmt import JsonFloat
from fauxcall.model import (
    ApiModel,
    CollectionModel,
    InvariantModel,
    RealTarget,
    RelationModel,
    SourceInfo,
    WorldRecord,
)
from fauxcall.world import World, check_world, records_from_values, resolve_relation_targets

if TYPE_CHECKING:
    from fauxcall.types import JsonValue
    from fauxcall.world import SessionWorld

NOW = datetime(2026, 10, 10, 14, 30, tzinfo=UTC)

EXCHANGES: list[JsonValue] = [
    {"code": "XNAS", "mic": "XNAS", "name": "Nasdaq"},
    {"code": "XNYS", "mic": "XNYS", "name": "New York Stock Exchange"},
    {"code": "LSE", "mic": "XLON", "name": "London Stock Exchange"},
]
TICKERS: list[JsonValue] = [
    {"symbol": "AAPL", "exchange": "XNAS", "mic": "XNAS", "isin": "US0378331005"},
    {"symbol": "MSFT", "exchange": "XNAS", "mic": "XNAS", "isin": "US5949181045"},
    {"symbol": "VOD", "exchange": "LSE", "mic": "XLON", "isin": None},
]
TAGS: list[JsonValue] = [{"id": 1, "name": "friendly"}, {"id": 2, "name": "guard"}]
PETS: list[JsonValue] = [
    {"id": 1, "name": "doggie", "tags": [{"id": 1, "name": "friendly"}], "owner": None},
    {"id": 2, "name": "rex", "tags": [{"id": "2", "name": "guard"}, {"id": 1, "name": "x"}]},
    {"id": 3, "name": "kitty", "tags": []},
]


def check_listed_tickers_have_isins(world: SessionWorld) -> list[str]:
    """A custom invariant, referenced by dotted path from the model."""
    return [
        f"tickers[{record.key!r}] has no ISIN"
        for record in world.list("tickers")
        if isinstance(record.record, dict) and record.record.get("isin") is None
    ]


def check_that_returns_a_string(world: SessionWorld) -> str:
    return f"{world.count('tickers')} tickers"


def check_that_crashes(world: SessionWorld) -> list[str]:
    raise RuntimeError("upstream data is corrupt")


class Checks:
    """Holds a check reachable through an attribute chain."""

    @staticmethod
    def everything_is_fine(_world: SessionWorld) -> list[str]:
        return []


def _model(
    relations: list[RelationModel] | None = None, invariants: list[InvariantModel] | None = None
) -> ApiModel:
    return ApiModel(
        name="markets",
        title="Markets API",
        source=SourceInfo(kind="openapi", url="https://api.example.test/spec", fetched_at=NOW),
        real=RealTarget(base_url="https://api.example.test"),
        collections=[
            CollectionModel(name="exchanges", key="code", key_type="str"),
            CollectionModel(name="tickers", key="symbol", key_type="str"),
            CollectionModel(name="tags"),
            CollectionModel(name="pets"),
        ],
        relations=relations or [],
        invariants=invariants or [],
    )


def _session(model: ApiModel) -> SessionWorld:
    values = {"exchanges": EXCHANGES, "tickers": TICKERS, "tags": TAGS, "pets": PETS}
    records = {
        name: records_from_values(model.collection(name), items) for name, items in values.items()
    }
    return World.from_records(model, records).session()


TICKER_EXCHANGE = RelationModel(
    name="ticker_exchange",
    from_collection="tickers",
    from_field="exchange",
    to_collection="exchanges",
)
PET_TAGS = RelationModel(
    name="pet_tags", from_collection="pets", from_field="tags[*].id", to_collection="tags"
)
TICKER_MIC = RelationModel(
    name="ticker_mic",
    from_collection="tickers",
    from_field="mic",
    to_collection="exchanges",
    to_field="mic",
)


def test_a_consistent_world_has_no_problems() -> None:
    model = _model(
        [TICKER_EXCHANGE, PET_TAGS, TICKER_MIC],
        [
            InvariantModel(
                name="unique_isin",
                kind="unique",
                collection="tickers",
                field="isin",
                description="ISINs identify one listing",
            ),
            InvariantModel(
                name="named_exchanges",
                kind="not_null",
                collection="exchanges",
                field="name",
                description="every exchange has a name",
            ),
            InvariantModel(
                name="named_tags",
                kind="not_null",
                collection="pets",
                field="tags[*].name",
                description="tags are named (pets may have none)",
            ),
        ],
    )
    assert check_world(model, _session(model)) == []


def test_relation_violations_after_writes() -> None:
    model = _model([TICKER_EXCHANGE, PET_TAGS, TICKER_MIC])
    session = _session(model)
    session.insert("tickers", {"symbol": "ZZZZ", "exchange": "XXXX", "mic": "XNAS"})
    session.insert("tickers", {"symbol": "OTC", "exchange": None, "mic": "XOTC"})
    session.update("pets", 3, {"tags": [{"id": 9, "name": "ghost"}, {"id": "abc"}]})
    session.delete("tags", 2)
    assert check_world(model, session) == [
        "relation 'ticker_exchange': tickers['ZZZZ'].exchange = 'XXXX' not found in exchanges",
        "relation 'pet_tags': pets[2].tags[0].id = '2' not found in tags",
        "relation 'pet_tags': pets[3].tags[0].id = 9 not found in tags",
        "relation 'pet_tags': pets[3].tags[1].id = 'abc' not found in tags",
        "relation 'ticker_mic': tickers['OTC'].mic = 'XOTC' not found in exchanges.mic",
    ]


def test_relations_match_keys_by_normalized_value_and_fields_by_raw_value() -> None:
    by_key_field = RelationModel(
        name="pet_tags_by_key_field",
        from_collection="pets",
        from_field="tags[*].id",
        to_collection="tags",
        to_field="$.id",
    )
    by_name = RelationModel(
        name="pet_tags_by_name",
        from_collection="pets",
        from_field="tags[*].id",
        to_collection="tags",
        to_field="name",
    )
    model = _model([PET_TAGS, by_key_field, by_name])
    problems = check_world(model, _session(model))
    assert not any("pet_tags'" in problem for problem in problems)  # "2" finds tag 2
    assert not any("pet_tags_by_key_field" in problem for problem in problems)
    assert (
        problems[0] == "relation 'pet_tags_by_name': pets[1].tags[0].id = 1 not found in tags.name"
    )


def test_optional_and_broken_relations() -> None:
    optional = RelationModel(
        name="optional",
        from_collection="tickers",
        from_field="exchange",
        to_collection="tags",
        required=False,
    )
    unknown = RelationModel(
        name="owners", from_collection="pets", from_field="owner", to_collection="owners"
    )
    bad_path = RelationModel(
        name="bad_path", from_collection="pets", from_field="tags[", to_collection="tags"
    )
    model = _model([optional, unknown, bad_path])
    problems = check_world(model, _session(model))
    assert problems[0].startswith("relation 'owners': unknown collection 'owners' (known: ")
    assert problems[1].startswith("relation 'bad_path': invalid JSON path 'tags['")
    assert len(problems) == 2


def test_unique_and_not_null_violations() -> None:
    model = _model(
        invariants=[
            InvariantModel(
                name="unique_isin",
                kind="unique",
                collection="tickers",
                field="isin",
                description="ISINs identify one listing",
            ),
            InvariantModel(
                name="unique_tag_names",
                kind="unique",
                collection="pets",
                field="tags[*].name",
                description="a tag name is used once",
            ),
            InvariantModel(
                name="isin_required",
                kind="not_null",
                collection="tickers",
                field="isin",
                description="every ticker has an ISIN",
            ),
            InvariantModel(
                name="tag_names",
                kind="not_null",
                collection="pets",
                field="tags[*].name",
                description="tags are named",
            ),
        ]
    )
    session = _session(model)
    session.insert("tickers", {"symbol": "AAPL2", "isin": "US0378331005"})
    session.insert("tickers", {"symbol": "NOISIN"})
    session.update("pets", 3, {"tags": [{"id": 1, "name": None}, {"id": 2, "name": "friendly"}]})
    assert check_world(model, session) == [
        (
            "invariant 'unique_isin': tickers['AAPL2'].isin = 'US0378331005' "
            "duplicates tickers['AAPL']"
        ),
        "invariant 'unique_tag_names': pets[3].tags[1].name = 'friendly' duplicates pets[1]",
        "invariant 'isin_required': tickers['VOD'].isin is null",
        "invariant 'isin_required': tickers['NOISIN'].isin is missing",
        "invariant 'tag_names': pets[3].tags[*].name is null",
    ]


def test_invariant_configuration_problems() -> None:
    model = _model(
        invariants=[
            InvariantModel(
                name="no_field", kind="unique", collection="tickers", description="broken"
            ),
            InvariantModel(
                name="no_collection",
                kind="not_null",
                collection="owners",
                field="name",
                description="broken",
            ),
            InvariantModel(
                name="bad_field",
                kind="not_null",
                collection="tickers",
                field="isin[",
                description="broken",
            ),
            InvariantModel(
                name="no_function", kind="custom", collection="tickers", description="broken"
            ),
        ]
    )
    problems = check_world(model, _session(model))
    assert problems[0] == "invariant 'no_field': a 'unique' invariant needs a field"
    assert problems[1].startswith("invariant 'no_collection': unknown collection 'owners'")
    assert problems[2].startswith("invariant 'bad_field': invalid JSON path 'isin['")
    assert problems[3] == "invariant 'no_function': a 'custom' invariant needs a function"
    assert len(problems) == 4


def _custom(function: str) -> InvariantModel:
    return InvariantModel(
        name="custom_check",
        kind="custom",
        collection="tickers",
        description="checked by code",
        function=function,
    )


NO_ISIN = ["invariant 'custom_check': tickers['VOD'] has no ISIN"]


@pytest.mark.parametrize(
    ("function", "expected"),
    [
        (f"{__name__}:check_listed_tickers_have_isins", NO_ISIN),
        (f"{__name__}.check_listed_tickers_have_isins", NO_ISIN),
        (f"{__name__}:Checks.everything_is_fine", []),
    ],
)
def test_custom_invariants_run_imported_functions(function: str, expected: list[str]) -> None:
    model = _model(invariants=[_custom(function)])
    assert check_world(model, _session(model)) == expected


@pytest.mark.parametrize(
    ("function", "message"),
    [
        (
            "fauxcall_no_such_package.checks:verify",
            (
                "cannot import 'fauxcall_no_such_package.checks:verify': "
                "ModuleNotFoundError: No module named 'fauxcall_no_such_package'"
            ),
        ),
        (
            f"{__name__}:missing_check",
            (
                f"cannot import '{__name__}:missing_check': "
                f"AttributeError: module '{__name__}' has no attribute 'missing_check'"
            ),
        ),
        (
            f"{__name__}:NOW",
            f"cannot import '{__name__}:NOW': TypeError: '{__name__}:NOW' is not callable",
        ),
        (
            "nodots",
            (
                "cannot import 'nodots': "
                "ValueError: expected 'package.module:function' or 'package.module.function'"
            ),
        ),
        (
            f"{__name__}:check_that_crashes",
            f"'{__name__}:check_that_crashes' raised RuntimeError: upstream data is corrupt",
        ),
        (
            f"{__name__}:check_that_returns_a_string",
            (
                f"'{__name__}:check_that_returns_a_string' must return a list of strings, "
                "not '3 tickers'"
            ),
        ),
        ("json:loads", "'json:loads' raised TypeError: "),  # wrong signature
    ],
)
def test_broken_custom_invariants_are_reported(function: str, message: str) -> None:
    model = _model(invariants=[_custom(function)])
    (problem,) = check_world(model, _session(model))
    assert problem.startswith(f"invariant 'custom_check': {message}")


def test_resolve_relation_targets() -> None:
    model = _model([TICKER_EXCHANGE, PET_TAGS, TICKER_MIC])
    session = _session(model)
    session.insert("exchanges", {"code": "XTKS", "mic": "XTKS", "name": "Tokyo"})
    session.insert("exchanges", {"code": "BATS", "mic": "XNAS", "name": "duplicate MIC"})
    session.update("tags", 2, {"id": None})  # the key is put back, never removed
    session.insert("tags", {"id": "7", "name": "calm"})
    assert resolve_relation_targets(model, session) == {
        "ticker_exchange": ["XNAS", "XNYS", "LSE", "XTKS", "BATS"],
        "pet_tags": [1, 2, "7"],  # values as the payloads spell them
        "ticker_mic": ["XNAS", "XNYS", "XLON", "XTKS"],
    }


def test_resolve_relation_targets_falls_back_to_stored_keys() -> None:
    model = _model([PET_TAGS])
    tags = [WorldRecord(key=1, record={"id": 1, "name": "friendly"}), WorldRecord(key=2, record={})]
    session = World.from_records(model, {"tags": tags}).session()
    assert resolve_relation_targets(model, session) == {"pet_tags": [1, 2]}


def test_resolve_relation_targets_copies_values_and_skips_nulls() -> None:
    nested = RelationModel(
        name="tag_names",
        from_collection="pets",
        from_field="label",
        to_collection="tags",
        to_field="name",
    )
    model = _model([nested])
    session = _session(model)
    session.insert("tags", {"id": 3, "name": None})
    session.insert("tags", {"id": 4, "name": {"en": "calm", "score": JsonFloat("0.50")}})
    targets = resolve_relation_targets(model, session)
    assert targets == {"tag_names": ["friendly", "guard", {"en": "calm", "score": 0.5}]}
    value = targets["tag_names"][2]
    assert isinstance(value, dict)
    value["en"] = "mutated"
    assert resolve_relation_targets(model, session)["tag_names"][2] == {"en": "calm", "score": 0.5}


def test_resolve_relation_targets_rejects_unknown_collections() -> None:
    unknown = RelationModel(
        name="owners", from_collection="pets", from_field="owner", to_collection="owners"
    )
    model = _model([unknown])
    with pytest.raises(WorldError, match="unknown collection 'owners'"):
        resolve_relation_targets(model, _session(model))
