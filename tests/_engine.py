"""A rich market-API model + world + recordings for engine tests (built in a temp dir)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Any

import httpx2
from pydantic import SecretStr

from fauxcall.clock import ControlledClock, ManualClock, datetime_to_ns
from fauxcall.config import AuthSettings, FakeSettings, IdentityConfig
from fauxcall.jsonfmt import JsonFloat, JsonStyle, loads
from fauxcall.model import (
    ApiModel,
    AuthModel,
    AuthScheme,
    Body,
    CalendarModel,
    CapturedRequest,
    CapturedResponse,
    CollectionModel,
    DurationModel,
    EndpointModel,
    ErrorTemplate,
    Exchange,
    JobModel,
    JobOutcome,
    JobStateModel,
    RateLimitModel,
    RealTarget,
    RelationModel,
    SessionWindow,
    SourceInfo,
    StreamEvent,
)
from fauxcall.model.world import WorldRecord
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime import FakeRuntime, create_app
from fauxcall.runtime.dynamic import dynamic_router
from fauxcall.storage import write_model
from fauxcall.types import HttpMethod
from fauxcall.world.io import save_collection

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from fauxcall.types import JsonObject, JsonValue

# Tuesday 2026-10-13 14:30 UTC = 10:30 New York (NYSE open).
OPEN_NS = datetime_to_ns(datetime(2026, 10, 13, 14, 30, tzinfo=UTC))
# Saturday 2026-10-10 12:00 UTC (closed); last close Friday 2026-10-09 16:00 New York = 20:00 UTC.
WEEKEND_NS = datetime_to_ns(datetime(2026, 10, 10, 12, 0, tzinfo=UTC))
FRIDAY_CLOSE = "2026-10-09T20:00:00.000000Z"
RECORDED_AT = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
API_KEY = "demo-key-123"


def j(value: object) -> Any:
    """View decoded JSON loosely, so tests can index into it without a cast per line."""
    return value


def envelope_error(code: str, status: int) -> ErrorTemplate:
    return ErrorTemplate(
        name=code,
        status=status,
        content_type="application/json",
        body_json={
            "error": {"code": code, "message": "{{param}} {{value}}", "request_id": "{{uuid}}"}
        },
    )


def market_model() -> ApiModel:
    iso6 = {"kind": "iso", "fraction_digits": 6, "tz_style": "Z"}
    endpoints: list[dict[str, Any]] = [
        {
            "id": "GET /v1/exchanges",
            "role": "list",
            "collection": "exchanges",
            "shape": {"kind": "list"},
        },
        {
            "id": "GET /v1/exchanges/{code}",
            "role": "get",
            "collection": "exchanges",
            "key_param": "code",
            "params": [
                {"name": "code", "location": "path", "required": True, "schema": {"type": "string"}}
            ],
            "errors": [{"when": "not_found", "template": "not_found"}],
        },
        {
            "id": "GET /v1/tickers",
            "role": "list",
            "collection": "tickers",
            "params": [
                {"name": "exchange", "location": "query", "maps_to": "exchange"},
                {
                    "name": "limit",
                    "location": "query",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                    "on_invalid": "error",
                },
                {"name": "cursor", "location": "query"},
            ],
            "errors": [{"when": "invalid_param", "template": "invalid_parameter"}],
            "shape": {
                "kind": "envelope",
                "items_path": "$.data",
                "template": {"data": [], "next_cursor": None, "total": 0},
            },
            "pagination": {
                "style": "cursor",
                "limit_param": "limit",
                "cursor_param": "cursor",
                "default_limit": 2,
                "max_limit": 100,
                "total_path": "$.total",
                "next_path": "$.next_cursor",
                "cursor_format": "key",
                "cursor_template": {"after": "{{key}}"},
                "cursor_encoding": "base64url",
            },
        },
        {
            "id": "GET /v1/tickers/{symbol}/quote",
            "role": "get",
            "collection": "tickers",
            "key_param": "symbol",
            "shape": {"kind": "record", "projection": ["symbol", "price", "as_of"]},
            "fields": [
                {
                    "kind": "calendar",
                    "path": "as_of",
                    "calendar": "nyse",
                    "event": "last_tick",
                    "lag_ms": 1500,
                    "format": iso6,
                }
            ],
            "errors": [{"when": "not_found", "template": "not_found"}],
        },
        {
            "id": "GET /v1/time",
            "role": "replay",
            "fields": [
                {
                    "kind": "now",
                    "path": "utc",
                    "scope": "body",
                    "format": {"kind": "iso", "fraction_digits": 7},
                },
                {
                    "kind": "now",
                    "path": "unix_ms",
                    "scope": "body",
                    "format": {"kind": "epoch", "epoch_unit": "ms"},
                },
                {"kind": "random", "path": "request_id", "scope": "body", "generator": "uuid4"},
            ],
            "auth_required": False,
        },
        {
            "id": "POST /v1/watchlists",
            "role": "create",
            "collection": "watchlists",
            "success_status": 201,
            "request_body": {
                "required": True,
                "validate_schema": True,
                "schema": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}, "symbols": {"type": "array"}},
                },
            },
            "write": {
                "status": 201,
                "id_start": 1001,
                "location_header": "/v1/watchlists/{{id}}",
                "defaults": {"created_at": "{{now:iso_ms}}", "symbols": []},
            },
            "errors": [{"when": "invalid_body", "template": "invalid_body"}],
        },
        {"id": "GET /v1/watchlists", "role": "list", "collection": "watchlists"},
        {
            "id": "GET /v1/watchlists/{id}",
            "role": "get",
            "collection": "watchlists",
            "key_param": "id",
            "errors": [{"when": "not_found", "template": "not_found"}],
        },
        {
            "id": "PUT /v1/watchlists/{id}",
            "role": "replace",
            "collection": "watchlists",
            "key_param": "id",
            "errors": [{"when": "not_found", "template": "not_found"}],
        },
        {
            "id": "PATCH /v1/watchlists/{id}",
            "role": "update",
            "collection": "watchlists",
            "key_param": "id",
            "errors": [{"when": "not_found", "template": "not_found"}],
        },
        {
            "id": "DELETE /v1/watchlists/{id}",
            "role": "delete",
            "collection": "watchlists",
            "key_param": "id",
            "success_status": 204,
            "write": {"echo": "empty", "status": 204},
            "errors": [{"when": "not_found", "template": "not_found"}],
        },
        {
            "id": "POST /v1/uploads",
            "role": "job_submit",
            "job": "upload",
            "success_status": 202,
            "write": {"id_strategy": "uuid4", "status": 202, "defaults": {"progress": 0}},
        },
        {
            "id": "GET /v1/uploads/{id}",
            "role": "job_status",
            "job": "upload",
            "key_param": "id",
            "errors": [{"when": "not_found", "template": "not_found"}],
        },
        {
            "id": "GET /v1/uploads/{id}/content",
            "role": "download",
            "collection": "uploads",
            "key_param": "id",
        },
        {"id": "GET /v1/search", "role": "search", "semantic": {"match": {"inputs": ["query.q"]}}},
        {"id": "GET /v1/stream/trades", "role": "sse", "stream": {"kind": "sse", "speed": 1000}},
        {
            "id": "GET /v1/ws/quotes",
            "role": "websocket",
            "stream": {"kind": "websocket", "respond_to": "match", "speed": 1000},
        },
    ]
    built: list[EndpointModel] = []
    for spec in endpoints:
        method, path = spec["id"].split(" ", 1)
        built.append(EndpointModel.model_validate({"method": method, "path": path, **spec}))
    return ApiModel(
        name="market",
        title="Market API",
        source=SourceInfo(
            kind="openapi", url="https://example.test/openapi.json", fetched_at=RECORDED_AT
        ),
        real=RealTarget(base_url="https://market.example.test"),
        json_style=JsonStyle(),
        endpoints=built,
        collections=[
            CollectionModel(name="exchanges", key="code", key_type="str"),
            CollectionModel(name="tickers", key="symbol", key_type="str"),
            CollectionModel(name="watchlists", key="id", key_type="int"),
            CollectionModel(name="uploads", key="id", key_type="uuid"),
        ],
        relations=[
            RelationModel(
                name="ticker_exchange",
                from_collection="tickers",
                from_field="exchange",
                to_collection="exchanges",
            )
        ],
        calendars=[
            CalendarModel(
                name="nyse",
                zone="America/New_York",
                sessions=[SessionWindow(open=time(9, 30), close=time(16, 0))],
                holidays=[],
            )
        ],
        jobs=[
            JobModel(
                name="upload",
                collection="uploads",
                state_field="status",
                progress_field="progress",
                states=[
                    JobStateModel(name="queued", duration=DurationModel(kind="fixed", seconds=2)),
                    JobStateModel(
                        name="processing",
                        duration=DurationModel(kind="fixed", seconds=6),
                        progress=(0, 100),
                    ),
                    JobStateModel(name="done", terminal=True, progress=(100, 100)),
                    JobStateModel(name="failed", terminal=True),
                ],
                outcomes=[JobOutcome(state="done", probability=1.0)],
                timestamps={"created_at": "created", "completed_at": "completed"},
            )
        ],
        auth=AuthModel(
            schemes=[AuthScheme(kind="api_key", location="header", name="X-API-Key")],
            missing_template="unauthorized",
            invalid_template="unauthorized",
            forbidden_template="forbidden",
        ),
        rate_limits=[
            RateLimitModel(
                name="per_key",
                limit=5,
                window_seconds=10,
                key="credential",
                endpoints=["GET /v1/exchanges*"],
                headers={
                    "X-RateLimit-Limit": "{{limit}}",
                    "X-RateLimit-Remaining": "{{remaining}}",
                },
                error_template="rate_limited",
            )
        ],
        errors=[
            envelope_error("not_found", 404),
            envelope_error("invalid_parameter", 400),
            envelope_error("invalid_body", 400),
            envelope_error("unauthorized", 401),
            envelope_error("forbidden", 403),
            envelope_error("rate_limited", 429),
            ErrorTemplate(
                name="unknown_route", status=404, content_type="text/plain", body_text="Not Found"
            ),
        ],
    )


def _exchange(
    exchange_id: str,
    endpoint: str,
    path: str,
    body: JsonValue,
    *,
    query: list[tuple[str, str]] | None = None,
    status: int = 200,
    stream: list[StreamEvent] | None = None,
    content_type: str = "application/json; charset=utf-8",
) -> Exchange:
    method = endpoint.split(" ", 1)[0]
    return Exchange(
        id=exchange_id,
        endpoint=endpoint,
        started_at=RECORDED_AT,
        started_ns=datetime_to_ns(RECORDED_AT),
        request=CapturedRequest(
            method=HttpMethod(method),
            url=f"https://market.example.test{path}",
            path=path,
            query=query or [],
        ),
        response=CapturedResponse(
            status=status,
            headers=[("content-type", content_type)],
            body=Body(kind="json", json_value=body) if body is not None else Body(kind="empty"),
        ),
        stream=stream or [],
    )


def write_data(data: Path, model: ApiModel) -> None:
    """World files + recordings for the market model."""
    exchanges: list[JsonObject] = [
        {"code": "XNYS", "name": "New York Stock Exchange", "timezone": "America/New_York"},
        {"code": "XLON", "name": "London Stock Exchange", "timezone": "Europe/London"},
    ]
    tickers: list[JsonObject] = [
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "exchange": "XNYS",
            "price": JsonFloat("227.50"),
            "as_of": "2026-10-09T19:59:58.500000Z",
        },
        {
            "symbol": "IBM",
            "name": "IBM",
            "exchange": "XNYS",
            "price": JsonFloat("101.10"),
            "as_of": "2026-10-09T19:59:58.500000Z",
        },
        {
            "symbol": "MSFT",
            "name": "Microsoft",
            "exchange": "XNYS",
            "price": JsonFloat("410.00"),
            "as_of": "2026-10-09T19:59:58.500000Z",
        },
        {
            "symbol": "VOD",
            "name": "Vodafone",
            "exchange": "XLON",
            "price": JsonFloat("0.72"),
            "as_of": "2026-10-09T15:29:58.500000Z",
        },
    ]
    for name, rows, key in (("exchanges", exchanges, "code"), ("tickers", tickers, "symbol")):
        save_collection(
            data / model.collection(name).path,
            [WorldRecord(key=row[key], record=row, recorded_at=RECORDED_AT) for row in rows],
        )
    store = RecordingStore(data / "recordings")
    store.save(
        _exchange(
            store.next_id(),
            "GET /v1/time",
            "/v1/time",
            loads(
                '{"utc":"2026-10-09T15:00:00.0000000Z","unix_ms":1791558000000,"request_id":"x","zone":"UTC"}'
            ),
        )
    )
    store.save(
        _exchange(
            store.next_id(),
            "GET /v1/search",
            "/v1/search",
            {"query": "apple", "results": [{"symbol": "AAPL"}]},
            query=[("q", "apple")],
        )
    )
    store.save(
        _exchange(
            store.next_id(),
            "GET /v1/search",
            "/v1/search",
            {"query": "big blue", "results": [{"symbol": "IBM"}]},
            query=[("q", "big blue")],
        )
    )
    trades = [
        StreamEvent(
            t_ms=10.0,
            kind="sse",
            event="trade",
            id="1",
            data='{"symbol":"AAPL","price":"227.51","time":"2026-10-09T15:00:00.010Z"}',
        ),
        StreamEvent(
            t_ms=210.0,
            kind="sse",
            event="trade",
            id="2",
            data='{"symbol":"AAPL","price":"227.49","time":"2026-10-09T15:00:00.210Z"}',
        ),
    ]
    store.save(
        _exchange(
            store.next_id(),
            "GET /v1/stream/trades",
            "/v1/stream/trades",
            None,
            query=[("symbol", "AAPL")],
            stream=trades,
            content_type="text/event-stream; charset=utf-8",
        )
    )
    ws = [
        StreamEvent(t_ms=0.0, kind="ws_send", data='{"action":"subscribe","symbols":["AAPL"]}'),
        StreamEvent(t_ms=5.0, kind="ws_recv", data='{"type":"subscribed","symbols":["AAPL"]}'),
        StreamEvent(
            t_ms=255.0, kind="ws_recv", data='{"type":"quote","symbol":"AAPL","price":"227.50"}'
        ),
        StreamEvent(t_ms=260.0, kind="ws_close", code=1000),
    ]
    store.save(_exchange(store.next_id(), "GET /v1/ws/quotes", "/v1/ws/quotes", None, stream=ws))


def with_endpoints(model: ApiModel, *specs: dict[str, Any]) -> ApiModel:
    """Add (or replace) endpoints given as ``{"id": "GET /path", ...}`` dictionaries."""
    for spec in specs:
        method, path = spec["id"].split(" ", 1)
        model.upsert_endpoint(
            EndpointModel.model_validate({"method": method, "path": path, **spec})
        )
    return model


def recorded(
    endpoint: str,
    *,
    query: list[tuple[str, str]] | None = None,
    response: CapturedResponse | None = None,
    stream: list[StreamEvent] | None = None,
) -> Exchange:
    """A recorded GET for ``endpoint`` (give it an id with :func:`save_exchange`)."""
    return Exchange(
        id="",
        endpoint=endpoint,
        started_at=RECORDED_AT,
        started_ns=datetime_to_ns(RECORDED_AT),
        request=CapturedRequest(
            method=HttpMethod.GET, url="x", path=endpoint.split(" ")[1], query=query or []
        ),
        response=response or CapturedResponse(status=200),
        stream=stream or [],
    )


def save_exchange(tmp_path: Path, exchange: Exchange) -> Exchange:
    """Store a recording in the harness data directory under the next free id."""
    store = RecordingStore(tmp_path / "data" / "recordings")
    saved = exchange.model_copy(update={"id": store.next_id()})
    store.save(saved)
    return saved


@dataclass(slots=True)
class Harness:
    """A running fake for tests."""

    app: FastAPI
    runtime: FakeRuntime
    base: ManualClock
    sleeps: list[float] = field(default_factory=list)

    def client(
        self, *, key: str | None = API_KEY, session: str | None = None
    ) -> httpx2.AsyncClient:
        headers = {}
        if key is not None:
            headers["X-API-Key"] = key
        if session is not None:
            headers["X-Fauxcall-Session"] = session
        transport = httpx2.ASGITransport(app=self.app)
        return httpx2.AsyncClient(transport=transport, base_url="http://fake.test", headers=headers)


def harness(
    tmp_path: Path, *, model: ApiModel | None = None, start_ns: int = OPEN_NS, **settings: Any
) -> Harness:
    """Build model + data under ``tmp_path`` and an app with a manual clock and recorded sleeps."""
    chosen = model or market_model()
    data = tmp_path / "data"
    write_data(data, chosen)
    write_model(data / "model.json", chosen)
    defaults: dict[str, Any] = {
        "data_dir": data,
        "latency": False,
        "auth": AuthSettings(
            users=[IdentityConfig(name="mallory", status="denied", api_key=SecretStr("denied-key"))]
        ),
        "log_level": "WARNING",
    }
    defaults.update(settings)
    config = FakeSettings(**defaults)
    base = ManualClock(start_ns)
    clock = ControlledClock(base)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    runtime = FakeRuntime(config, chosen, clock=clock, sleep=fake_sleep)
    app = create_app(
        config, routers=[dynamic_router(chosen)], runtime=runtime, configure_logs=False
    )
    return Harness(app=app, runtime=runtime, base=base, sleeps=sleeps)
