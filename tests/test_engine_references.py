"""Writes keep references as the real API does: refused, restricted, cascaded or cleared."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request

from _engine import Harness, harness, j, market_model, with_endpoints
from fauxcall.model import ErrorTemplate, RelationModel

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.model import ApiModel
    from fauxcall.runtime import RequestContext


def rules(model: ApiModel, **ticker_exchange: object) -> ApiModel:
    """The market model, with exchanges that can be deleted and symbols that must exist."""
    model = with_endpoints(
        model,
        {
            "id": "DELETE /v1/exchanges/{code}",
            "role": "delete",
            "collection": "exchanges",
            "key_param": "code",
            "success_status": 204,
            "write": {"echo": "empty", "status": 204},
        },
        {"id": "GET /v1/symbols/{symbol}", "role": "get", "collection": "tickers"},
    )
    model.relations[0] = RelationModel.model_validate(
        {
            "name": "ticker_exchange",
            "from_collection": "tickers",
            "from_field": "exchange",
            "to_collection": "exchanges",
            **ticker_exchange,
        }
    )
    model.relations.append(
        RelationModel(
            name="watchlist_symbols",
            from_collection="watchlists",
            from_field="symbols[*]",
            to_collection="tickers",
            on_missing="refuse",
            on_delete="set_null",
        )
    )
    return model


async def test_writes_pointing_at_nothing_are_refused(tmp_path: Path) -> None:
    model = rules(market_model())
    model.errors.append(
        ErrorTemplate(name="unknown_symbol", status=404, body_json={"error": "no {{value}}"})
    )
    create = next(e for e in model.endpoints if e.id == "POST /v1/watchlists")
    create.errors.append(
        create.errors[0].model_copy(
            update={"when": "missing_reference", "template": "unknown_symbol"}
        )
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        refused = await client.post("/v1/watchlists", json={"name": "x", "symbols": ["NOPE"]})
        assert (refused.status_code, refused.json()) == (404, {"error": "no NOPE"})  # recorded
        made = await client.post("/v1/watchlists", json={"name": "x", "symbols": ["AAPL"]})
        assert made.status_code == 201
        patched = await client.patch(f"/v1/watchlists/{made.json()['id']}", json={"symbols": ["Z"]})
        assert patched.status_code == 422  # no recorded error for PATCH: a generic refusal
        assert j(patched.json())["error"] == "missing_reference"
        listed = (await client.get("/v1/watchlists")).json()
        assert len(listed) == 1  # nothing refused was stored


async def test_deleting_what_is_still_referenced_is_restricted(tmp_path: Path) -> None:
    fake = harness(tmp_path, model=rules(market_model(), on_delete="restrict"))
    async with fake.client() as client:
        refused = await client.delete("/v1/exchanges/XLON")  # Vodafone trades there
        assert refused.status_code == 409
        assert j(refused.json())["error"] == "still_referenced"
        assert (await client.get("/v1/symbols/VOD")).status_code == 200  # nothing changed


async def test_deletes_cascade_and_clear_references(tmp_path: Path) -> None:
    fake = harness(tmp_path, model=rules(market_model(), on_delete="cascade"))
    async with fake.client() as client:
        made = await client.post(
            "/v1/watchlists", json={"name": "mixed", "symbols": ["VOD", "AAPL"]}
        )
        assert (await client.delete("/v1/exchanges/XLON")).status_code == 204
        assert (await client.get("/v1/symbols/VOD")).status_code == 404  # cascaded
        watchlist = (await client.get(f"/v1/watchlists/{made.json()['id']}")).json()
        assert watchlist["symbols"] == ["AAPL"]  # Vodafone left the list


def context_for(fake: Harness, endpoint_id: str) -> RequestContext:
    """A request context like a custom handler's, for the endpoint ``endpoint_id``."""
    method, path = endpoint_id.split(" ", 1)
    scope = {"type": "http", "method": method, "path": path, "headers": [], "query_string": b""}
    return fake.runtime.new_context(Request(scope), fake.runtime.model.endpoint(endpoint_id))


async def test_custom_handlers_keep_references_too(tmp_path: Path) -> None:
    model = rules(market_model(), on_missing="refuse", on_delete="restrict")
    fake = harness(tmp_path, model=model)
    ctx = context_for(fake, "DELETE /v1/exchanges/{code}")
    refused = ctx.delete("exchanges", "XLON")
    assert refused is not None
    assert refused.status_code == 409
    assert ctx.delete("exchanges", "NOPE") is None  # nothing to delete: left to the handler
    dangling = ctx.refuse_missing("tickers", {"symbol": "X", "exchange": "NOPE"})
    assert dangling is not None
    assert dangling.status_code == 422
    assert ctx.refuse_missing("tickers", {"symbol": "X", "exchange": "XLON"}) is None
    cascading = harness(tmp_path / "b", model=rules(market_model(), on_delete="cascade"))
    ctx = context_for(cascading, "DELETE /v1/exchanges/{code}")
    assert ctx.delete("exchanges", "XLON") is None
    assert ctx.world.get("tickers", "VOD") is None  # Vodafone went with its exchange


def test_custom_endpoints_count_as_writes() -> None:
    model = market_model()
    model.relations[0].min_per_target = 1
    assert model.undecided() == []  # nothing writes tickers or deletes exchanges
    custom = with_endpoints(
        market_model(),
        {"id": "POST /v1/tickers", "role": "custom", "collection": "tickers"},
        {"id": "DELETE /v1/exchanges/{code}", "role": "computed", "collection": "exchanges"},
    )
    custom.relations[0].min_per_target = 1
    assert [u.split(": ")[1].split(" ")[0] for u in custom.undecided()] == [
        "on_missing",
        "on_delete",
    ]
