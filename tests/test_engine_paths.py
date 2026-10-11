"""Engine branches the market-API scenarios don't reach: odd write semantics, templates, replay,
chunked streams, routing quirks, faults, rate-limit keys, spec serving, Jev-decided outcomes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Literal

import httpx2
import pytest
from fastapi import APIRouter, HTTPException, Query

from _engine import harness, market_model, recorded, save_exchange, with_endpoints
from fauxcall.config import FakeSettings, FaultRule
from fauxcall.model import (
    Body,
    CapturedResponse,
    ErrorTemplate,
    RateLimitModel,
    StreamEvent,
)
from fauxcall.model.semantic import OutcomeSpec, SemanticModel
from fauxcall.runtime import FakeRuntime, create_app, register_tool
from fauxcall.runtime.dynamic import dynamic_router
from fauxcall.runtime.handlers import order_like
from fauxcall.runtime.jev import JevAnswer
from fauxcall.storage import write_model

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonValue


async def test_echo_only_writes_like_fakerestapi(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {
            "id": "POST /v1/books",
            "role": "create",
            "collection": "watchlists",
            "write": {"persist": False, "echo": "request", "id_strategy": "client"},
        },
        {"id": "GET /v1/books/{id}", "role": "get", "collection": "watchlists", "key_param": "id"},
        {
            "id": "PUT /v1/books/{id}",
            "role": "replace",
            "collection": "watchlists",
            "key_param": "id",
            "write": {"persist": False, "echo": "request", "upsert": True},
        },
        {
            "id": "DELETE /v1/books/{id}",
            "role": "delete",
            "collection": "watchlists",
            "key_param": "id",
            "write": {"persist": False, "echo": "empty"},
        },
        {"id": "POST /v1/echo", "role": "echo"},
    )
    fake = harness(tmp_path, model=model)
    json_type = {"content-type": "application/json"}
    async with fake.client() as client:
        created = await client.post("/v1/books", json={"id": 999, "title": "probe"})
        assert created.status_code == 200
        assert created.json() == {"id": 999, "title": "probe"}
        assert (await client.get("/v1/books/999")).status_code == 404  # never persisted
        assert (await client.post("/v1/books", json={"title": "no id"})).status_code == 400
        replaced = await client.put("/v1/books/5", json={"title": "x"})
        assert replaced.json() == {"title": "x"}
        assert (await client.delete("/v1/books/12345")).status_code == 200
        echoed = await client.post("/v1/echo", content=b'{"a":[1,2.50]}', headers=json_type)
        assert echoed.text == '{"a":[1,2.50]}'  # the float keeps its spelling
        bad = await client.post("/v1/echo", content=b"{", headers=json_type)
        assert bad.status_code == 400


async def test_message_echo_ids_and_conflicts(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {
            "id": "POST /v1/exchanges",
            "role": "create",
            "collection": "exchanges",
            "write": {"id_strategy": "client", "status": 201},
        },
        {
            "id": "DELETE /v1/exchanges/{code}",
            "role": "delete",
            "collection": "exchanges",
            "key_param": "code",
            "write": {
                "echo": "message",
                "message_template": {"code": 200, "message": "deleted {{id}}"},
            },
        },
        {
            "id": "POST /v1/tokens",
            "role": "create",
            "collection": "uploads",
            "write": {"id_strategy": "uuid4"},
        },
        {
            "id": "POST /v1/codes",
            "role": "create",
            "collection": "watchlists",
            "write": {"id_strategy": "random_int"},
        },
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        made = await client.post("/v1/exchanges", json={"code": "XPAR", "name": "Paris"})
        assert made.status_code == 201
        again = await client.post("/v1/exchanges", json={"code": "XPAR", "name": "again"})
        assert again.status_code == 409
        deleted = await client.delete("/v1/exchanges/XPAR")
        assert deleted.json() == {"code": 200, "message": "deleted XPAR"}
        token = (await client.post("/v1/tokens", json={})).json()
        assert len(token["id"]) == 36
        code = (await client.post("/v1/codes", json={})).json()
        assert code["id"] >= 10**8


async def test_replay_without_match_and_no_match_template(tmp_path: Path) -> None:
    model = market_model()
    model.errors.append(
        ErrorTemplate(
            name="nothing", status=404, content_type="text/plain", body_text="no {{param.q}}"
        )
    )
    with_endpoints(
        model,
        {"id": "GET /v1/replay", "role": "replay"},
        {
            "id": "GET /v1/fuzzy",
            "role": "replay",
            "semantic": {
                "match": {"inputs": ["query.q"], "fallback": "none", "no_match_template": "nothing"}
            },
        },
    )
    fake = harness(tmp_path, model=model)
    save_exchange(
        tmp_path,
        recorded(
            "GET /v1/fuzzy",
            query=[("q", "zzz")],
            response=CapturedResponse(status=200, body=Body(kind="text", text="plain answer")),
        ),
    )
    async with fake.client() as client:
        assert (await client.get("/v1/replay")).status_code == 404  # no recordings at all
        miss = await client.get("/v1/fuzzy", params={"q": "completely different words"})
        assert miss.status_code == 404
        assert miss.text == "no completely different words"


async def test_generic_errors_and_body_less_templates(tmp_path: Path) -> None:
    model = market_model()
    model.endpoint("GET /v1/exchanges/{code}").errors = []
    model.errors = [e for e in model.errors if e.name != "not_found"]
    model.errors.append(ErrorTemplate(name="empty_error", status=410))
    with_endpoints(
        model,
        {
            "id": "GET /v1/gone/{code}",
            "role": "get",
            "collection": "exchanges",
            "key_param": "code",
            "errors": [{"when": "not_found", "template": "empty_error"}],
        },
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        generic = await client.get("/v1/exchanges/NOPE")
        assert generic.status_code == 404
        assert generic.json() == {"error": "not_found", "message": "not found", "param": "code"}
        empty = await client.get("/v1/gone/NOPE")
        assert empty.status_code == 410
        assert empty.content == b""


async def test_chunked_stream_replays_the_recorded_chunks(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {"id": "GET /v1/chunks", "role": "sse", "stream": {"kind": "chunked", "speed": 1000}},
    )
    fake = harness(tmp_path, model=model)
    save_exchange(
        tmp_path,
        recorded(
            "GET /v1/chunks",
            response=CapturedResponse(
                status=200, headers=[("content-type", "application/x-ndjson")]
            ),
            stream=[
                StreamEvent(t_ms=1, kind="chunk", data='{"n":1}'),
                StreamEvent(t_ms=2, kind="chunk", data="tail"),
            ],
        ),
    )
    async with fake.client() as client:
        chunked = await client.get("/v1/chunks")
    assert chunked.text == '{"n":1}tail'
    assert chunked.headers["content-type"] == "application/x-ndjson"


async def test_routing_quirks_case_and_trailing_slash(tmp_path: Path) -> None:
    model = market_model()
    model.routing.case_insensitive = True
    model.routing.trailing_slash = "ignore"
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        assert (await client.get("/V1/EXCHANGES/XNYS")).json()["code"] == "XNYS"
        assert (await client.get("/v1/exchanges/")).status_code == 200


async def test_faults_disconnect_and_template_errors(tmp_path: Path) -> None:
    rule = FaultRule(
        endpoint="GET /v1/exchanges", kind="error", status=500, error_template="not_found"
    )
    fake = harness(tmp_path, faults=[rule])
    async with fake.client() as client:
        response = await client.get("/v1/exchanges")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "not_found"
        disconnect = FaultRule(endpoint="*", kind="disconnect").model_dump(mode="json")
        await client.put("/__fauxcall/faults", json=[disconnect])
        with pytest.raises((httpx2.HTTPError, ConnectionAbortedError)):
            await client.get("/v1/exchanges/XNYS")


@pytest.mark.parametrize("key_kind", ["ip", "session", "global", "credential"])
async def test_rate_limit_keys(
    tmp_path: Path, key_kind: Literal["global", "ip", "credential", "session"]
) -> None:
    model = market_model()
    model.rate_limits = [
        RateLimitModel(
            name="tiny",
            limit=1,
            window_seconds=60,
            key=key_kind,
            endpoints=["GET /v1/exchanges/*"],
            algorithm="sliding_window",
            retry_after=False,
            headers_on_success=False,
        )
    ]
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        assert (await client.get("/v1/exchanges/XNYS")).status_code == 200
        second = await client.get("/v1/exchanges/XNYS")
        assert second.status_code == 429
        assert "retry-after" not in second.headers
    async with fake.client(key="another-key", session="other") as other:
        # Only a limit keyed on the caller (credential or session) lets a different caller in.
        expected = 200 if key_kind in {"credential", "session"} else 429
        assert (await other.get("/v1/exchanges/XNYS")).status_code == expected


async def test_validation_and_http_errors_from_custom_routes(tmp_path: Path) -> None:
    model = market_model()
    data = tmp_path / "data"
    write_model(data / "model.json", model)
    settings = FakeSettings(data_dir=data, latency=False, log_level="WARNING")
    custom = APIRouter()

    @custom.get("/custom/typed")
    async def typed(limit: Annotated[int, Query()]) -> dict[str, int]:
        return {"limit": limit}

    @custom.get("/custom/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="short and stout")

    runtime = FakeRuntime(settings, model)
    app = create_app(settings, routers=[custom], runtime=runtime, configure_logs=False)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client:
        invalid = await client.get("/custom/typed", params={"limit": "x"})
        assert invalid.status_code == 400
        assert invalid.json()["error"] == "invalid_param"  # no such template: the generic shape
        teapot_response = await client.get("/custom/teapot")
        assert teapot_response.status_code == 418
        assert teapot_response.text == "short and stout"
    with pytest.raises(ValueError, match="no MCP server"):
        register_tool(app, "x", None)  # type: ignore[arg-type]


async def test_spec_paths_serve_the_snapshot(tmp_path: Path) -> None:
    model = market_model()
    model.source.spec_file = "source/openapi.json"
    model.routing.spec_paths = ["/openapi.json"]
    fake = harness(tmp_path, model=model)
    spec = tmp_path / "data" / "source" / "openapi.json"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text('{"openapi": "3.1.0"}')
    async with fake.client() as client:
        served = await client.get("/openapi.json")
        assert served.json() == {"openapi": "3.1.0"}
        assert served.headers["content-type"] == "application/json"
    yaml_model = market_model()
    yaml_model.source.spec_file = "source/none.yaml"
    yaml_model.routing.spec_paths = ["/spec.yaml"]
    missing = harness(tmp_path / "other", model=yaml_model)
    async with missing.client() as client:
        assert (await client.get("/spec.yaml")).status_code == 404


class _StubJev:
    active = True
    cached_answers = 0

    def __init__(self, answer: JsonValue) -> None:
        self.answer = answer

    async def choose(
        self, *, instructions: str, options: dict[str, str], state: JsonValue
    ) -> JevAnswer | None:
        del instructions, options, state
        if self.answer is None:
            return None
        return JevAnswer(kind="choice", value=self.answer, confidence=0.9)

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("answer", "expected"), [("failed", "failed"), (None, "done"), ("bogus", "done")]
)
async def test_jev_decides_job_outcomes(tmp_path: Path, answer: JsonValue, expected: str) -> None:
    model = market_model()
    model.job("upload").outcome_decider = "jev"
    model.endpoint("POST /v1/uploads").semantic = SemanticModel(
        outcome=OutcomeSpec(
            instructions="Would it fail?",
            inputs=["body.name"],
            outcomes={"done": "ok", "failed": "corrupt"},
            fallback="done",
        )
    )
    fake = harness(tmp_path, model=model)
    fake.runtime.jev = _StubJev(answer)  # type: ignore[assignment]
    async with fake.client() as client:
        job_id = (await client.post("/v1/uploads", json={"name": "data.bin"})).json()["id"]
        await client.post("/__fauxcall/clock", json={"op": "advance", "seconds": 60})
        # No answer, or one that isn't a terminal state, falls back to the spec's fallback.
        assert (await client.get(f"/v1/uploads/{job_id}")).json()["status"] == expected


def test_order_like_handles_nested_and_non_objects() -> None:
    schema: JsonValue = {"properties": {"id": {}, "meta": {"properties": {"b": {}, "a": {}}}}}
    value: JsonValue = {"meta": {"a": 1, "b": 2, "c": 3}, "x": 0, "id": 9}
    ordered = json.dumps(order_like(value, schema))
    assert ordered == '{"id": 9, "meta": {"b": 2, "a": 1, "c": 3}, "x": 0}'
    assert order_like([1], schema) == [1]
    assert order_like({"a": 1}, {"type": "object"}) == {"a": 1}


def test_dynamic_router_registers_every_endpoint() -> None:
    routes = {getattr(r, "path", None) for r in dynamic_router(market_model()).routes}
    assert {"/v1/ws/quotes", "/v1/exchanges/{code}"} <= routes


@pytest.mark.parametrize(("empty_as_absent", "status"), [(True, 200), (False, 400)])
async def test_empty_parameter_values(
    tmp_path: Path, *, empty_as_absent: bool, status: int
) -> None:
    limit = {
        "name": "limit",
        "location": "query",
        "schema": {"type": "integer"},
        "on_invalid": "error",
        "empty_as_absent": empty_as_absent,
    }
    model = with_endpoints(
        market_model(),
        {"id": "GET /v1/limited", "role": "list", "collection": "exchanges", "params": [limit]},
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        assert (await client.get("/v1/limited?limit=")).status_code == status
