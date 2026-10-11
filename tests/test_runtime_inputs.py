from __future__ import annotations

from typing import TYPE_CHECKING

from _engine import harness, market_model
from fauxcall.model import CapturedRequest, CapturedResponse, EndpointModel, Exchange
from fauxcall.model.recording import Body
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime.handlers import exchange_inputs
from fauxcall.runtime.inputs import json_inputs, path_inputs, query_inputs
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonValue


def test_json_inputs_keep_every_array_element_in_order() -> None:
    body: JsonValue = {
        "symbols": ["AAPL", "MSFT"],
        "order": {"qty": 5, "legs": [{"side": "buy"}, {"side": "sell"}]},
        "odd key": True,
        "empty": [],
    }
    assert json_inputs("body", body) == {
        "body.symbols[*]": ["AAPL", "MSFT"],
        "body.order.qty": 5,
        "body.order.legs[*].side": ["buy", "sell"],
        'body["odd key"]': True,
    }
    assert list(json_inputs("args", body)) == [
        "args.symbols[*]",
        "args.order.qty",
        "args.order.legs[*].side",
        'args["odd key"]',
    ]
    assert json_inputs("body", ["not", "an", "object"]) == {}
    assert json_inputs("body", None) == {}


def test_query_inputs_keep_repeated_parameters() -> None:
    pairs = [("symbol", "AAPL"), ("limit", "5"), ("symbol", "MSFT")]
    assert query_inputs(pairs) == {"query.symbol": ["AAPL", "MSFT"], "query.limit": "5"}
    assert query_inputs([]) == {}


def test_path_inputs_are_text() -> None:
    assert path_inputs({"id": 7, "code": "XNYS"}) == {"path.id": "7", "path.code": "XNYS"}


def test_recorded_exchange_inputs() -> None:
    exchange = Exchange(
        id="1",
        endpoint="POST /v1/orders/{id}",
        started_at="2026-10-09T15:00:00Z",  # type: ignore[arg-type]
        started_ns=0,
        request=CapturedRequest(
            method=HttpMethod.POST,
            url="x",
            path="/v1/orders/9",
            path_params={"id": "9"},
            query=[("tag", "a"), ("tag", "b")],
            body=Body(kind="json", json_value={"items": [{"sku": "x"}, {"sku": "y"}]}),
        ),
    )
    assert exchange_inputs(exchange) == {
        "path.id": "9",
        "query.tag": ["a", "b"],
        "body.items[*].sku": ["x", "y"],
    }


async def test_replay_distinguishes_repeated_query_values(tmp_path: Path) -> None:
    model = market_model()
    model.upsert_endpoint(
        EndpointModel.model_validate(
            {
                "id": "GET /v1/compare",
                "method": "GET",
                "path": "/v1/compare",
                "role": "replay",
                "semantic": {"match": {"inputs": ["query.symbol"], "fallback": "none"}},
            }
        )
    )
    fake = harness(tmp_path, model=model)
    store = RecordingStore(tmp_path / "data" / "recordings")
    for symbols, text in ((["AAPL", "MSFT"], "both"), (["AAPL"], "apple")):
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint="GET /v1/compare",
                started_at="2026-10-09T15:00:00Z",  # type: ignore[arg-type]
                started_ns=0,
                request=CapturedRequest(
                    method=HttpMethod.GET,
                    url="x",
                    path="/v1/compare",
                    query=[("symbol", s) for s in symbols],
                ),
                response=CapturedResponse(status=200, body=Body(kind="text", text=text)),
            )
        )
    async with fake.client() as client:
        both = await client.get("/v1/compare", params=[("symbol", "AAPL"), ("symbol", "MSFT")])
        assert both.text == "both"
        apple = await client.get("/v1/compare", params={"symbol": "AAPL"})
        assert apple.text == "apple"


async def test_dashed_path_parameters_match_their_recordings(tmp_path: Path) -> None:
    model = market_model()
    model.upsert_endpoint(
        EndpointModel.model_validate(
            {
                "id": "GET /v1/pets/{pet-id}",
                "method": "GET",
                "path": "/v1/pets/{pet-id}",
                "role": "replay",
            }
        )
    )
    fake = harness(tmp_path, model=model)
    store = RecordingStore(tmp_path / "data" / "recordings")
    for pet, status, text in (("1", 200, "Rex"), ("999", 404, "Pet not found")):
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint="GET /v1/pets/{pet-id}",
                started_at="2026-10-09T15:00:00Z",  # type: ignore[arg-type]
                started_ns=0,
                request=CapturedRequest(
                    method=HttpMethod.GET,
                    url="x",
                    path=f"/v1/pets/{pet}",
                    path_params={"pet-id": pet},
                ),
                response=CapturedResponse(status=status, body=Body(kind="text", text=text)),
            )
        )
    async with fake.client() as client:
        missing = await client.get("/v1/pets/999")
        assert (missing.status_code, missing.text) == (404, "Pet not found")
        found = await client.get("/v1/pets/1")
        assert (found.status_code, found.text) == (200, "Rex")
