"""The custom handlers, in-process (fast mode), checked against the recordings and the clock."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.client import placeholder_credentials
from fauxcall.testing.support import settings_for_tests
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fauxcall_refapi import handlers
from fauxcall_refapi.app import build_app
from fauxcall_refapi.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from fauxcall.model.recording import Exchange

DATA = Settings().data_dir
EXCHANGES: list[Exchange] = [
    exchange for _, exchange in RecordingStore(DATA / "recordings").iter_exchanges()
]


def _needs_a_recorded_resource(exchange: Exchange) -> bool:
    """An unknown symbol for a watchlist that existed while recording (fresh fake: 404 first)."""
    body = exchange.response.body.json_value if exchange.response is not None else None
    error = body.get("error") if isinstance(body, dict) else None
    return (
        isinstance(error, dict)
        and error.get("code") == "unknown_symbol"
        and "{id}" in exchange.endpoint
    )


def _recorded(endpoint: str, *, statuses: range = range(200, 300)) -> list[Exchange]:
    return [
        e
        for e in EXCHANGES
        if e.endpoint == endpoint
        and e.response is not None
        and e.response.status in statuses
        and e.purpose != "auth"
        and "{{" not in e.request.path
        and not _needs_a_recorded_resource(e)
    ]


@pytest.fixture
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


def _http(app: FastAPI) -> httpx2.AsyncClient:
    headers = placeholder_credentials(runtime_of_app(app).model.auth).headers
    transport = httpx2.ASGITransport(app=app)
    return httpx2.AsyncClient(transport=transport, base_url="http://fake.test", headers=headers)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with _http(app) as http:
        yield http


@pytest.fixture
async def limited() -> AsyncIterator[httpx2.AsyncClient]:
    """Rate limits emulated (fast mode turns them off), latency still off."""
    async with _http(build_app(settings_for_tests(Settings, fast=False, latency=False))) as http:
        yield http


async def _freeze(client: httpx2.AsyncClient, at: str) -> None:
    response = await client.post("/__fauxcall/clock", json={"op": "freeze", "at": at})
    assert response.status_code == 200


async def _advance(client: httpx2.AsyncClient, seconds: float) -> None:
    response = await client.post("/__fauxcall/clock", json={"op": "advance", "seconds": seconds})
    assert response.status_code == 200


def _body(exchange: Exchange) -> Any:
    assert exchange.response is not None
    return exchange.response.body.json_value


def _request_kwargs(exchange: Exchange) -> dict[str, Any]:
    """The recorded request's query and body, for replaying it with the harness credentials."""
    request = exchange.request
    kwargs: dict[str, Any] = {"params": list(request.query)}
    body = request.body
    content_type = next((v for k, v in request.headers if k.lower() == "content-type"), None)
    if body.kind == "json":
        kwargs["content"] = json.dumps(body.json_value).encode()
    elif body.kind == "text":
        kwargs["content"] = (body.text or "").encode()
    if content_type is not None:
        kwargs["headers"] = {"content-type": content_type}
    return kwargs


# -- exact answers, as recorded -------------------------------------------------------------------


async def test_every_recorded_search_scores_the_same(client: httpx2.AsyncClient) -> None:
    recorded = _recorded("GET /v1/search")
    assert len(recorded) > 150
    for exchange in recorded:
        response = await client.get("/v1/search", params=list(exchange.request.query))
        assert response.json() == _body(exchange), exchange.request.query


async def test_every_recorded_ticker_page_is_the_same(client: httpx2.AsyncClient) -> None:
    for exchange in _recorded("GET /v1/tickers"):
        response = await client.get("/v1/tickers", params=list(exchange.request.query))
        assert response.json() == _body(exchange), exchange.request.query


@pytest.mark.parametrize(
    "endpoint",
    [
        "GET /v1/exchanges/{code}",
        "GET /v1/tickers",
        "GET /v1/tickers/{symbol}",
        "GET /v1/tickers/{symbol}/quote",
        "GET /v1/search",
        "GET /v1/stream/trades",
        "GET /v1/watchlists/{id}",
        "POST /v1/watchlists",
        "PUT /v1/watchlists/{id}",
        "PATCH /v1/watchlists/{id}",
        "DELETE /v1/watchlists/{id}",
        "GET /v1/uploads/{id}",
        "DELETE /v1/uploads/{id}",
    ],
)
async def test_recorded_errors_are_the_same(client: httpx2.AsyncClient, endpoint: str) -> None:
    """Status, code, message and param of every recorded 4xx (not the request id)."""
    recorded = [
        e
        for e in _recorded(endpoint, statuses=range(400, 409))
        if e.request.body.kind != "multipart"
    ]
    assert recorded
    for exchange in recorded:
        method = exchange.request.method.value
        response = await client.request(method, exchange.request.path, **_request_kwargs(exchange))
        real = _body(exchange)["error"]
        fake = response.json()["error"]
        assert response.status_code == exchange.response.status, exchange.id  # type: ignore[union-attr]
        del real["request_id"], fake["request_id"]
        assert fake == real, exchange.id


async def test_quotes_carry_the_recorded_prices(client: httpx2.AsyncClient) -> None:
    quotes = _recorded("GET /v1/tickers/{symbol}/quote")
    assert len({_body(e)["symbol"] for e in quotes}) == 60
    for exchange in quotes:
        body = (await client.get(exchange.request.path)).json()
        expected = _body(exchange)
        assert (body["symbol"], body["price"], body["currency"]) == (
            expected["symbol"],
            expected["price"],
            expected["currency"],
        )


async def test_exchanges_and_tickers_are_case_insensitive(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/v1/exchanges/xnys")).json()["code"] == "XNYS"
    assert (await client.get("/v1/tickers/air.pa")).json()["symbol"] == "AIR.PA"
    missing = (await client.get("/v1/tickers/zzzz")).json()["error"]
    assert missing["message"] == "No ticker with symbol 'ZZZZ'."
    exchanges = (await client.get("/v1/exchanges")).json()
    assert [e["code"] for e in exchanges] == ["XHKG", "XLON", "XNAS", "XNYS", "XPAR", "XTKS"]


# -- time ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "symbol", "state", "as_of"),
    [
        ("2026-10-15T01:00:00Z", "7203.T", "open", "2026-10-15T00:59:58.500000Z"),
        ("2026-10-15T01:00:00Z", "AAPL", "closed", "2026-10-14T20:00:00.000000Z"),
        ("2026-10-15T01:00:00Z", "0005.HK", "closed", "2026-10-14T08:00:00.000000Z"),
        ("2026-10-15T01:00:00Z", "AIR.PA", "closed", "2026-10-14T15:30:00.000000Z"),
        ("2026-10-15T03:00:00Z", "7203.T", "closed", "2026-10-15T02:30:00.000000Z"),  # lunch
        ("2026-10-15T04:30:00Z", "0005.HK", "closed", "2026-10-15T04:00:00.000000Z"),  # lunch
        ("2026-10-17T12:00:00Z", "AAPL", "closed", "2026-10-16T20:00:00.000000Z"),  # Saturday
        ("2026-10-19T12:00:00Z", "MSFT", "closed", "2026-10-16T20:00:00.000000Z"),  # Monday 8am
        ("2026-10-19T14:00:00.123456Z", "BA", "open", "2026-10-19T13:59:58.623456Z"),
        ("2026-11-02T21:30:00Z", "AAPL", "closed", "2026-11-02T21:00:00.000000Z"),  # after DST
        ("2026-10-15T09:00:00Z", "BP.L", "open", "2026-10-15T08:59:58.500000Z"),
    ],
)
async def test_quote_time_follows_the_exchange(
    client: httpx2.AsyncClient, at: str, symbol: str, state: str, as_of: str
) -> None:
    await _freeze(client, at)
    body = (await client.get(f"/v1/tickers/{symbol}/quote")).json()
    assert (body["market_state"], body["as_of"]) == (state, as_of)


async def test_server_time_is_the_clock(client: httpx2.AsyncClient) -> None:
    await _freeze(client, "2026-10-15T00:40:39.505272Z")
    body = (await client.get("/v1/time", params={"zone": "Asia/Tokyo"})).json()
    assert body == {"utc": "2026-10-15T00:40:39.5052720Z", "unix_ms": 1792024839505, "zone": "UTC"}


# -- watchlists -----------------------------------------------------------------------------------


async def test_watchlist_lifecycle(client: httpx2.AsyncClient) -> None:
    await _freeze(client, "2026-10-15T00:44:09.034Z")
    created = await client.post(
        "/v1/watchlists", json={"name": "probe", "symbols": ["msft", "AAPL", "aapl"], "x": 1}
    )
    assert created.status_code == 201
    assert created.headers["location"] == "/v1/watchlists/1001"
    assert created.json() == {
        "id": 1001,
        "name": "probe",
        "symbols": ["MSFT", "AAPL"],
        "created_at": "2026-10-15T00:44:09.034Z",
        "updated_at": "2026-10-15T00:44:09.034Z",
    }
    assert (await client.post("/v1/watchlists", json={"name": "two"})).json()["id"] == 1002
    assert (await client.get("/v1/watchlists/01001")).json()["name"] == "probe"
    await _advance(client, 5)
    replaced = (
        await client.put("/v1/watchlists/1001", json={"name": "r", "symbols": ["tsla"]})
    ).json()
    assert (replaced["symbols"], replaced["updated_at"]) == (["TSLA"], "2026-10-15T00:44:14.034Z")
    await _advance(client, 1)
    patched = (await client.patch("/v1/watchlists/1001", json={})).json()
    assert (patched["name"], patched["updated_at"]) == ("r", "2026-10-15T00:44:15.034Z")
    patched = (await client.patch("/v1/watchlists/1001", json={"symbols": ["7203.t"]})).json()
    assert (patched["name"], patched["symbols"]) == ("r", ["7203.T"])
    assert [w["id"] for w in (await client.get("/v1/watchlists")).json()] == [1001, 1002]
    assert (await client.delete("/v1/watchlists/1001")).status_code == 204
    assert (await client.delete("/v1/watchlists/1001")).status_code == 404
    assert (await client.get("/v1/watchlists/1001")).status_code == 404
    created = await client.post("/v1/watchlists", json={"name": "three"})
    assert created.json()["id"] == 1003  # ids are never reused


async def test_watchlist_writes_need_a_full_key(limited: httpx2.AsyncClient) -> None:
    client = limited
    readonly = {"X-API-Key": "readonly-key"}
    refused = await client.post("/v1/watchlists", json={"name": "x"}, headers=readonly)
    assert refused.status_code == 403
    assert refused.json()["error"]["message"] == (
        "This API key is read-only; POST requests are not allowed."
    )
    assert "x-ratelimit-remaining" in refused.headers  # counted, like the real API (000032)
    assert (await client.get("/v1/watchlists", headers=readonly)).status_code == 200
    revoked = await client.get("/v1/watchlists", headers={"X-API-Key": "revoked-key"})
    assert revoked.json()["error"]["code"] == "api_key_revoked"
    assert "x-ratelimit-remaining" not in revoked.headers
    unknown = await client.get("/v1/watchlists", headers={"X-API-Key": "nope"})
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (401, "invalid_api_key")


async def test_watchlist_bodies(client: httpx2.AsyncClient) -> None:
    async def code(content: bytes, content_type: str | None) -> str:
        headers = {"content-type": content_type} if content_type else {}
        response = await client.post("/v1/watchlists", content=content, headers=headers)
        return str(response.json()["error"]["code"])

    assert await code(b'{"name": "x"}', "text/plain") == "invalid_body"
    assert await code(b"", "application/json") == "invalid_body"
    assert await code(b"{bad", "application/json") == "invalid_json"
    assert await code(b"[]", "application/json") == "invalid_body"
    assert await code(b"null", "application/json") == "invalid_body"
    assert await code(b'{"name": "x"}', None) == "invalid_body"
    for content_type in (
        "application/json; charset=utf-8",
        "application/vnd.api+json",
        "Application/JSON",
    ):
        accepted = await client.post(
            "/v1/watchlists",
            content=b'{"name": "c", "symbols": ["v"]}',
            headers={"content-type": content_type},
        )
        assert accepted.json()["symbols"] == ["V"]


async def test_unknown_watchlist_beats_unknown_symbol(client: httpx2.AsyncClient) -> None:
    patched = await client.patch("/v1/watchlists/999999", json={"symbols": ["ZZZZ"]})
    assert patched.json()["error"]["code"] == "watchlist_not_found"
    replaced = await client.put("/v1/watchlists/999999", json={"name": "x", "symbols": ["ZZZZ"]})
    assert replaced.json()["error"]["code"] == "watchlist_not_found"
    invalid = await client.put("/v1/watchlists/999999", json={"name": ""})
    assert invalid.json()["error"]["param"] == "name"


# -- uploads --------------------------------------------------------------------------------------


async def _upload(client: httpx2.AsyncClient, text: str, **form: str) -> dict[str, Any]:
    files = {"file": ("positions.csv", text.encode(), "text/csv")}
    response = await client.post("/v1/uploads", files=files, data=form)
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_upload_job_timeline(client: httpx2.AsyncClient) -> None:
    await _freeze(client, "2026-10-15T00:45:30.731627Z")
    upload = await _upload(client, "symbol,qty\nAAPL,10\n", label="p")
    assert upload["status"] == "queued"
    assert (upload["label"], upload["created_at"]) == ("p", "2026-10-15T00:45:30.731627+00:00")
    content = await client.get(f"/v1/uploads/{upload['id']}/content")
    assert content.json()["error"]["message"] == (
        f"Upload '{upload['id']}' is queued; content is available only when status is done."
    )
    await _advance(client, 3.2)
    state = (await client.get(f"/v1/uploads/{upload['id']}")).json()
    assert (state["status"], state["progress"]) == ("processing", 20)
    await _advance(client, 5)
    state = (await client.get(f"/v1/uploads/{upload['id']}")).json()
    assert (state["status"], state["progress"]) == ("done", 100)
    assert state["completed_at"] == "2026-10-15T00:45:38.731627+00:00"
    content = await client.get(f"/v1/uploads/{upload['id']}/content")
    assert content.content == b"symbol,qty\nAAPL,10\n"
    assert content.headers["content-type"] == "text/csv"
    assert content.headers["content-disposition"] == 'attachment; filename="positions.csv"'


async def test_corrupt_uploads_fail(client: httpx2.AsyncClient) -> None:
    await _freeze(client, "2026-10-15T00:46:00Z")
    bad = await _upload(client, "a\nCORRUPT\n")
    fine = await _upload(client, "this file is corrupt but lower case\n", label="")
    assert fine["label"] is None
    await _advance(client, 8)
    listed = (await client.get("/v1/uploads")).json()
    assert [u["id"] for u in listed] == [fine["id"], bad["id"]]  # newest first
    assert listed[1]["status"] == "failed"
    assert listed[1]["error"] == "corrupt_file"
    assert listed[0]["status"] == "done"
    assert "error" not in listed[0]
    refused = await client.get(f"/v1/uploads/{bad['id']}/content")
    assert refused.status_code == 409
    assert (await client.delete(f"/v1/uploads/{bad['id']}")).status_code == 204
    assert (await client.get(f"/v1/uploads/{bad['id']}")).status_code == 404


async def test_upload_refusals(client: httpx2.AsyncClient) -> None:
    async def error(**kwargs: Any) -> tuple[int, str]:
        response = await client.post("/v1/uploads", **kwargs)
        return response.status_code, str(response.json()["error"]["code"])

    big = {"file": ("big.bin", b"a" * (handlers.MAX_UPLOAD + 1), "application/octet-stream")}
    assert await error(files=big) == (413, "payload_too_large")
    assert await error(json={"file": "x"}) == (400, "missing_parameter")
    assert await error(data={"label": "x"}, files={"document": ("a", b"x")}) == (
        400,
        "missing_parameter",
    )
    assert await error(data={"file": "just text"}) == (400, "invalid_parameter")
    assert await error(files={"file": ("", b"x", "text/plain")}) == (400, "invalid_parameter")
    two = [
        ("file", ("first.txt", b"1", "text/plain")),
        ("file", ("second.txt", b"22", "text/plain")),
    ]
    last = await client.post("/v1/uploads", files=two)
    assert (last.json()["filename"], last.json()["size"]) == ("second.txt", 2)
    exact = {"file": ("exact.bin", b"a" * handlers.MAX_UPLOAD)}
    assert (await client.post("/v1/uploads", files=exact)).status_code == 202


# -- streams --------------------------------------------------------------------------------------


async def test_trades_replay_the_symbols_walk(app: FastAPI, client: httpx2.AsyncClient) -> None:
    response = await client.get("/v1/stream/trades", params={"symbol": "aapl", "count": "3"})
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    frames = [frame for frame in response.text.split("\n\n") if frame]
    assert len(frames) == 3
    first = dict(line.split(": ", 1) for line in frames[0].split("\n"))
    assert (first["event"], first["id"]) == ("trade", "1")
    data = json.loads(first["data"])
    assert (data["symbol"], data["price"], data["size"]) == ("AAPL", "227.50", 189)
    pauses = runtime_of_app(app).pauses
    assert all(pause <= handlers.TRADE_SPACING_S for pause in pauses)
    walk = handlers.trade_walk(_ctx_stub(app), "7203.T")
    assert len(walk) == 50


def _ctx_stub(app: FastAPI) -> Any:
    class Stub:
        runtime = runtime_of_app(app)

    return Stub()


async def test_trade_errors(client: httpx2.AsyncClient) -> None:
    async def error(params: dict[str, str]) -> str:
        response = await client.get("/v1/stream/trades", params=params)
        return str(response.json()["error"]["message"])

    assert await error({"symbol": "ZZZZ", "count": "0"}) == (
        "Invalid value for parameter 'count': must be an integer between 1 and 50."
    )
    assert await error({"symbol": "zzzz"}) == "Unknown symbol 'zzzz'."
    assert await error({}) == "Missing required parameter 'symbol'."


def _ws_url(app: FastAPI, key: str | None = None) -> str:
    if key is None:
        key = placeholder_credentials(runtime_of_app(app).model.auth).headers["X-API-Key"]
    return f"/v1/ws/quotes?api_key={key}"


def test_ws_subscribe_streams_five_rounds(app: FastAPI) -> None:
    with TestClient(app) as http, http.websocket_connect(_ws_url(app)) as ws:
        ws.send_json({"action": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_json({"action": "subscribe", "symbols": ["aapl", "AAPL", "7203.t"]})
        assert ws.receive_json() == {"type": "subscribed", "symbols": ["AAPL", "7203.T"]}
        quotes = [ws.receive_json() for _ in range(10)]
        assert {q["symbol"] for q in quotes} == {"AAPL", "7203.T"}
        assert quotes[0]["price"] == "227.48"
        assert ws.receive_json() == {"type": "done"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1000


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("not json", "Invalid JSON."),
        ('["subscribe"]', "Messages must be JSON objects."),
        ('{"action": "dance"}', 'Unknown action: "dance".'),
        ('{"action": 5}', "Unknown action: 5."),
        ("{}", "Unknown action: null."),
        ('{"action": "subscribe", "symbols": []}', "symbols must be a non-empty array of strings."),
        (
            '{"action": "subscribe", "symbols": ["AAPL", 1]}',
            "symbols must be a non-empty array of strings.",
        ),
        ('{"action": "subscribe", "symbols": ["zzzz", "yyyy"]}', "Unknown symbol 'zzzz'."),
    ],
)
def test_ws_errors_keep_the_socket_open(app: FastAPI, message: str, error: str) -> None:
    with TestClient(app) as http, http.websocket_connect(_ws_url(app)) as ws:
        ws.send_text(message)
        assert ws.receive_json() == {"type": "error", "message": error}
        ws.send_bytes(b"\x00\x01")
        assert ws.receive_json() == {
            "type": "error",
            "message": "Binary messages are not supported.",
        }
        ws.send_json({"action": "ping"})
        assert ws.receive_json() == {"type": "pong"}


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("", "Missing api_key query parameter."),
        ("not-a-key", "Invalid API key."),
        ("revoked-key", "This API key has been revoked."),
    ],
)
def test_ws_refuses_bad_keys_after_accepting(app: FastAPI, key: str, reason: str) -> None:
    with TestClient(app) as http, http.websocket_connect(_ws_url(app, key)) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert (closed.value.code, closed.value.reason) == (4401, reason)


def test_ws_client_may_leave(app: FastAPI) -> None:
    with TestClient(app) as http, http.websocket_connect(_ws_url(app)) as ws:
        ws.send_json({"action": "ping"})
        assert ws.receive_json() == {"type": "pong"}


# -- search internals and limits ------------------------------------------------------------------


def test_tokens_and_scores() -> None:
    assert handlers.tokens("Moët Hennessy") == ["mo", "t", "hennessy"]
    toyota: dict[str, Any] = {
        "symbol": "7203.T",
        "name": "Toyota Motor Corporation",
        "sector": "Automotive",
    }
    assert handlers.score("toyota motor corp", toyota) == Decimal("0.733")
    assert handlers.score("cars", toyota) == Decimal("0.5")
    assert handlers.score("  ", toyota) == 0


async def test_rate_limit_is_thirty_per_ten_seconds(limited: httpx2.AsyncClient) -> None:
    client = limited
    await _freeze(client, "2026-10-15T01:00:00Z")
    statuses = [(await client.get("/v1/time")).status_code for _ in range(31)]
    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429
    blocked = await client.get("/v1/time")
    assert int(blocked.headers["retry-after"]) >= 1
    assert (await client.get("/healthz")).status_code == 200
    await _advance(client, 10)
    assert (await client.get("/v1/time")).status_code == 200


def test_isoformat_without_microseconds() -> None:
    at = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp()) * 1_000_000
    assert handlers._isoformat(at) == "2026-10-15T00:00:00+00:00"


# -- routing under /v1 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        ("GET", "/v1/nothing", "", 401, "missing_api_key"),
        ("GET", "/v1", "", 401, "missing_api_key"),
        ("GET", "/v1x", "", 404, "not_found"),
        ("GET", "/v1/nothing", "nobody", 401, "invalid_api_key"),
        ("GET", "/v1/nothing", "revoked-key", 403, "api_key_revoked"),
        ("GET", "/v1/nothing", "readonly-key", 404, "not_found"),
        ("OPTIONS", "/v1/tickers", "", 401, "missing_api_key"),
        ("GET", "/nothing", "", 404, "not_found"),
        ("GET", "/healthz/", "", 404, "not_found"),
    ],
)
async def test_unrouted_requests_authenticate_first(
    client: httpx2.AsyncClient, case: tuple[str, str, str, int, str]
) -> None:
    method, path, key, status, code = case
    response = await client.request(method, path, headers={"X-API-Key": key})
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)


async def test_routed_requests_pass_through(client: httpx2.AsyncClient) -> None:
    response = await client.get("/v1/time", headers={"X-API-Key": ""})
    assert response.json()["error"]["code"] == "missing_api_key"
    assert (await client.get("/v1/time")).status_code == 200


@pytest.mark.parametrize(
    ("path", "allow"),
    [
        ("/v1/tickers", "GET"),
        ("/v1/watchlists", "GET, POST"),
        ("/v1/watchlists/1", "GET, PUT, PATCH, DELETE"),
        ("/v1/uploads/x", "GET, DELETE"),
    ],
)
async def test_wrong_methods_list_the_allowed_ones(
    client: httpx2.AsyncClient, path: str, allow: str
) -> None:
    response = await client.request("OPTIONS", path)
    assert (response.status_code, response.headers["allow"]) == (405, allow)
    assert response.json()["error"]["message"] == f"Method OPTIONS is not allowed for {path}."


async def test_head_answers_have_no_body(client: httpx2.AsyncClient) -> None:
    revoked = await client.head("/v1/exchanges", headers={"X-API-Key": "revoked-key"})
    assert (revoked.status_code, revoked.content) == (403, b"")
    refused = await client.head("/v1/exchanges")
    assert (refused.status_code, refused.content, refused.headers["allow"]) == (405, b"", "GET")


async def test_unrouted_refusals_are_journaled(client: httpx2.AsyncClient) -> None:
    await client.get("/v1/nothing", headers={"X-API-Key": ""})
    entries = (await client.get("/__fauxcall/journal")).json()
    entries = entries.get("entries", entries) if isinstance(entries, dict) else entries
    assert any(e["path"] == "/v1/nothing" and e["status"] == 401 for e in entries)


async def test_rate_limit_headers_count_every_v1_call(limited: httpx2.AsyncClient) -> None:
    """Like 000002-000011: Limit 30, Remaining counts down, unrouted calls count too."""
    first = await limited.get("/v1/time")
    assert first.headers["x-ratelimit-limit"] == "30"
    assert first.headers["x-ratelimit-remaining"] == "29"
    reset = int(first.headers["x-ratelimit-reset"])
    assert 9 <= reset - int(first.json()["unix_ms"] / 1000) <= 11
    unrouted = await limited.get("/v1/nothing")
    assert (unrouted.status_code, unrouted.headers["x-ratelimit-remaining"]) == (404, "28")
    assert (await limited.get("/healthz")).headers.get("x-ratelimit-remaining") is None
    assert (await limited.get("/v1/exchanges")).headers["x-ratelimit-remaining"] == "27"
    for _ in range(27):
        await limited.get("/v1/nothing")
    blocked = await limited.get("/v1/nothing")
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (429, "rate_limited")
    assert int(blocked.headers["retry-after"]) >= 1


async def test_spec_is_the_rest_document(client: httpx2.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert spec["info"]["title"] == "FauxCall Reference Market API"
    assert len(spec["paths"]) == 14
    assert "servers" not in spec


async def test_malformed_multipart(client: httpx2.AsyncClient) -> None:
    for content_type, body, message in [
        ("multipart/form-data", b"file=x", "Missing boundary in multipart."),
        (
            "multipart/form-data; boundary=zzz",
            b"--zzz\r\nnot a part\r\n",
            "Invalid multipart data.",
        ),
    ]:
        response = await client.post(
            "/v1/uploads", content=body, headers={"content-type": content_type}
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert (error["code"], error["message"]) == ("bad_request", message)
        assert "param" not in error


async def test_download_filename_is_ascii(client: httpx2.AsyncClient) -> None:
    await _freeze(client, "2026-10-15T01:00:00Z")
    files = {"file": ('résumé "q".txt', b"x\n", "text/plain")}
    upload = (await client.post("/v1/uploads", files=files)).json()
    await _advance(client, 9)
    content = await client.get(f"/v1/uploads/{upload['id']}/content")
    disposition = content.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="r_sum_ ')
    assert disposition.isascii()


async def test_calendars_follow_the_exchanges(app: FastAPI, client: httpx2.AsyncClient) -> None:
    calendars = {c.name: c for c in runtime_of_app(app).model.calendars}
    for exchange in (await client.get("/v1/exchanges")).json():
        calendar = calendars[exchange["code"]]
        assert calendar.zone == exchange["timezone"]
        assert [(f"{s.open:%H:%M}", f"{s.close:%H:%M}") for s in calendar.sessions] == [
            (s["open"], s["close"]) for s in exchange["sessions"]
        ]


async def test_ws_as_of_follows_the_clock(app: FastAPI, client: httpx2.AsyncClient) -> None:
    await _freeze(client, "2026-10-15T01:00:00Z")
    with TestClient(app) as http, http.websocket_connect(_ws_url(app)) as ws:
        ws.send_json({"action": "subscribe", "symbols": ["7203.T", "AAPL"]})
        ws.receive_json()
        tokyo, apple = ws.receive_json(), ws.receive_json()
    assert tokyo["as_of"] == "2026-10-15T00:59:58.500000Z"
    assert apple["as_of"] == "2026-10-14T20:00:00.000000Z"
