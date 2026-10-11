"""Models survive being saved and read back (``write_model`` leaves out default values)."""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

from pydantic import TypeAdapter

from _engine import market_model
from fauxcall.model import ApiModel
from fauxcall.model.fields import FieldPolicy
from fauxcall.storage import model_to_json, read_model, write_model

if TYPE_CHECKING:
    from pathlib import Path

POLICIES = TypeAdapter(list[FieldPolicy])
EXAMPLES: list[dict[str, object]] = [
    {"kind": "static", "path": "$.a"},
    {"kind": "now", "path": "$.t", "format": {"kind": "iso"}},
    {"kind": "relative", "path": "$.due", "format": {"kind": "iso"}},
    {"kind": "calendar", "path": "$.t", "calendar": "nyse", "format": {"kind": "iso"}},
    {"kind": "periodic", "path": "$.t", "period_seconds": 60, "format": {"kind": "iso"}},
    {"kind": "random", "path": "$.id", "generator": "uuid4"},
    {"kind": "counter", "path": "$.seq"},
    {"kind": "echo", "path": "$.base", "source": "query.base"},
    {"kind": "series", "path": "$.price", "series": "aapl"},
    {"kind": "volatile", "path": "$.price"},
    {"kind": "pseudonym", "path": "$.email", "category": "email"},
    {"kind": "computed", "path": "$.total", "function": "total"},
    {"kind": "ignore", "path": "$.debug"},
]


def test_examples_cover_every_policy_kind() -> None:
    union = get_args(get_args(FieldPolicy)[0])
    kinds = {cls.model_fields["kind"].default for cls in union}
    assert kinds == {example["kind"] for example in EXAMPLES}


def test_every_policy_kind_survives_a_lean_dump() -> None:
    policies = POLICIES.validate_python(EXAMPLES)
    dumped = [model_to_json(policy) for policy in policies]
    assert all(isinstance(d, dict) and next(iter(d)) == "kind" for d in dumped)  # kind first
    assert POLICIES.validate_python(dumped) == policies


def test_a_rich_model_round_trips_through_disk(tmp_path: Path) -> None:
    model = market_model()
    assert any(endpoint.fields for endpoint in model.endpoints)
    write_model(tmp_path / "model.json", model)
    assert read_model(tmp_path / "model.json", ApiModel) == model
