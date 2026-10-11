"""RequestContext on its own: zones, request values, placeholders, headers, errors, bodies."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from starlette.requests import Request
from starlette.websockets import WebSocket

from _engine import RECORDED_AT, harness, j, market_model
from fauxcall.model import ErrorTemplate
from fauxcall.model.endpoints import HeaderRule, ResponseVariant
from fauxcall.model.world import WorldRecord
from fauxcall.runtime.context import EarlyResponse, RequestContext
from fauxcall.runtime.handlers import job_for, lookup, path_key, render_job
from fauxcall.runtime.templating import UNRESOLVED

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from pathlib import Path

    from _engine import Harness
    from fauxcall.types import JsonValue


def _scope(fake: Harness, **overrides: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/v1/tickers/AAPL/quote",
        "raw_path": b"/v1/tickers/AAPL/quote",
        "query_string": b"tz=Asia/Tokyo&bad=Not/AZone&n=3",
        "headers": [(b"x-thing", b"v"), (b"content-type", b"application/json")],
        "path_params": {"symbol": "AAPL"},
        "app": fake.app,
        "server": ("fake.test", 80),
        "scheme": "http",
        "root_path": "",
    }
    scope.update(overrides)
    return scope


def _context(
    fake: Harness, endpoint: str | None = "GET /v1/tickers/{symbol}/quote", **scope: Any
) -> RequestContext:
    request = Request(_scope(fake, **scope))
    model = fake.runtime.model
    return fake.runtime.new_context(request, model.endpoint(endpoint) if endpoint else None)


def test_zones(tmp_path: Path) -> None:
    ctx = _context(harness(tmp_path))
    ctx.params["region"] = "Europe/Paris"
    ctx.params["count"] = 3
    document: JsonValue = {"zone": "America/Chicago"}
    assert str(ctx.zone("UTC", None)) == "UTC"
    assert str(ctx.zone("Europe/London", None)) == "Europe/London"
    assert str(ctx.zone("param:tz", None)) == "Asia/Tokyo"  # from the query
    assert str(ctx.zone("param:region", None)) == "Europe/Paris"  # parsed parameter wins
    assert str(ctx.zone("param:symbol", None)) == "UTC"  # "AAPL" is no zone
    assert str(ctx.zone("param:bad", None)) == "UTC"
    assert str(ctx.zone("param:count", None)) == "UTC"  # not text
    assert str(ctx.zone("param:absent", None)) == "UTC"
    assert str(ctx.zone("field:zone", document)) == "America/Chicago"
    assert str(ctx.zone("field:missing", document)) == "UTC"
    assert str(ctx.zone("calendar", None)) == "UTC"


def test_request_values_and_counters(tmp_path: Path) -> None:
    ctx = _context(harness(tmp_path))
    ctx.params["n"] = 3
    ctx.json_body = {"order": {"qty": 5}}
    assert ctx.request_value("query.tz") == "Asia/Tokyo"
    assert ctx.request_value("path.symbol") == "AAPL"
    assert ctx.request_value("header.x-thing") == "v"
    assert ctx.request_value("param.n") == 3
    assert ctx.request_value("body.order.qty") == 5
    assert ctx.request_value("body") == {"order": {"qty": 5}}
    assert ctx.request_value("cookie.x") is None
    assert ctx.header("x-missing") is None
    assert [ctx.next_counter("seq", 10, 5) for _ in range(3)] == [10, 15, 20]
    assert ctx.series_value("nope", "hold") is None


def test_placeholders(tmp_path: Path) -> None:
    ctx = _context(harness(tmp_path))
    ctx.json_body = {"name": "Ann"}
    resolve = ctx.resolver(extra="x", symbol="overridden")
    assert resolve("extra", None) == "x"
    assert resolve("symbol", None) == "overridden"  # explicit values come first
    assert re.fullmatch(r"[0-9a-f-]{36}", str(resolve("uuid", None)))
    assert str(resolve("uuid7", None))[14] == "7"
    assert str(resolve("traceparent", None)).startswith("00-")
    assert len(str(resolve("hex", "8"))) == 8
    assert len(str(resolve("digits", None))) == 8
    assert len(str(resolve("alnum", "4"))) == 4
    assert resolve("now", "date") == "2026-10-13"
    assert resolve("now", "nonsense") == resolve("now", "iso")
    assert resolve("method", None) == "GET"
    assert resolve("path", None) == "/v1/tickers/AAPL/quote"
    assert resolve("query", None) == "tz=Asia/Tokyo&bad=Not/AZone&n=3"
    assert str(resolve("url", None)).startswith("http://fake.test/v1/tickers/AAPL/quote?")
    assert resolve("identity", None) is None
    assert resolve("param.symbol", None) == "AAPL"
    assert resolve("param.tz", None) == "Asia/Tokyo"
    ctx.params["parsed"] = 7
    assert resolve("param.parsed", None) == 7
    assert resolve("header.x-thing", None) == "v"
    assert resolve("body.name", None) == "Ann"
    assert resolve("other.thing", None) is UNRESOLVED
    assert resolve("unknown", None) is UNRESOLVED


def test_headers_variants_and_responses(tmp_path: Path) -> None:
    model = market_model()
    model.default_headers = {"x-api-version": "2026-10-01", "x-flag": "{{flag}}"}
    endpoint = model.endpoint("GET /v1/tickers/{symbol}/quote")
    endpoint.responses = [
        ResponseVariant(status=200, headers={"x-request-id": HeaderRule(value="{{request_id}}")})
    ]
    ctx = _context(harness(tmp_path, model=model))
    ok = ctx.respond({"a": 1}, values={"flag": True})
    assert ok.headers["x-api-version"] == "2026-10-01"
    assert ok.headers["x-flag"] == "true"  # JSON spelling, not Python's True
    assert ok.headers["x-request-id"] == ctx.request_id
    raw = ctx.respond_bytes(b"abc", content_type="text/csv", status=200)
    assert (raw.body, raw.headers["content-type"]) == (b"abc", "text/csv")
    untyped = ctx.respond_bytes(b"abc")
    assert "content-type" not in untyped.headers
    assert ctx.empty().status_code == 204
    assert ctx.variant(404) is None
    no_endpoint = _context(harness(tmp_path / "other"), endpoint=None)
    assert no_endpoint.variant(200) is None
    assert no_endpoint.endpoint_id == ""


def test_error_templates(tmp_path: Path) -> None:
    model = market_model()
    model.errors += [
        ErrorTemplate(
            name="text_error",
            status=422,
            content_type="text/plain",
            body_text="bad {{param}}={{value}} flag={{flag}}",
            headers={"x-error": "{{status}}"},
        ),
        ErrorTemplate(name="untyped_text", status=400, body_text="plain"),
        ErrorTemplate(name="typed_empty", status=410, content_type="application/problem+json"),
        ErrorTemplate(
            name="json_error", status=409, body_json={"error": "{{param}}", "n": "{{value}}"}
        ),
    ]
    ctx = _context(harness(tmp_path, model=model))
    text = ctx.error("custom", template="text_error", param="q", value=2, values={"flag": None})
    assert text.status_code == 422
    assert text.body == b"bad q=2 flag=null"
    assert text.headers["x-error"] == "422"
    assert "content-type" not in ctx.error("custom", template="untyped_text").headers
    typed = ctx.error("custom", template="typed_empty")
    assert (typed.status_code, typed.body, typed.headers["content-type"]) == (
        410,
        b"",
        "application/problem+json",
    )
    as_json = ctx.error("conflict", template="json_error", param="name", value=3, status=400)
    assert (as_json.status_code, j(as_json.body)) == (400, b'{"error":"name","n":3}')
    generic = ctx.error("payload_too_large", headers={"x-extra": "1"})
    assert (generic.status_code, generic.headers["x-extra"]) == (413, "1")
    early = ctx.fail("not_found", param="symbol", value="ZZZ", extra="x")
    assert isinstance(early, EarlyResponse)
    assert early.response.status_code == 404
    assert early.args == (404,)


async def test_bodies(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    ctx = _context(fake)

    async def receive_broken() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{", "more_body": False}

    ctx.request = Request(_scope(fake, method="POST"), receive_broken)
    await ctx.load_body()
    assert ctx.json_error
    assert ctx.json_body is None

    async def receive_never() -> dict[str, Any]:  # pragma: no cover - must not be read
        raise AssertionError

    async def send_never(message: MutableMapping[str, Any]) -> None:  # pragma: no cover
        raise AssertionError(message)

    ws_scope = _scope(fake, type="websocket", path="/v1/ws/quotes")
    socket_ctx = fake.runtime.new_context(WebSocket(ws_scope, receive_never, send_never), None)
    await socket_ctx.load_body()  # nothing to read on a WebSocket
    assert socket_ctx.body_bytes == b""


def test_render_record_projection_and_handler_guards(tmp_path: Path) -> None:
    model = market_model()
    model.endpoint("GET /v1/tickers/{symbol}/quote").shape.projection = ["symbol", "price"]
    fake = harness(tmp_path, model=model)
    ctx = _context(fake)
    record = WorldRecord(key="AAPL", record={"symbol": "AAPL", "price": "1", "name": "Apple"})
    assert ctx.render_record(record) == {"symbol": "AAPL", "price": "1"}
    bare = _context(fake, endpoint=None)
    assert bare.render_record(WorldRecord(key=1, record=[1, 2], recorded_at=RECORDED_AT)) == [1, 2]
    assert path_key(bare) is None
    assert lookup(ctx, "tickers", None) is None
    assert job_for(ctx, None) is None
    job = fake.runtime.model.job("upload")
    odd = WorldRecord(key="5f0e9d2a-0000-4000-8000-000000000000", record=["not", "an", "object"])
    assert render_job(ctx, job, odd) is odd


@pytest.mark.parametrize(
    "arg", ["iso", "iso_ms", "iso_us", "iso_7", "iso_ns", "http_date", "epoch", "epoch_ms", "date"]
)
def test_now_formats(tmp_path: Path, arg: str) -> None:
    ctx = _context(harness(tmp_path))
    value = ctx.resolver()("now", arg)
    assert value not in (None, "")
