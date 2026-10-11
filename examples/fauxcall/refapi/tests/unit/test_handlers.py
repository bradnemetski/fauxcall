"""Custom handlers, in-process: stateful flows, clock-driven states and the parsing rules."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx2
import pytest

from fauxcall_refapi import handlers
from fauxcall_refapi.app import build_app
from fauxcall_refapi.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fauxcall.types import JsonObject

ADMIN = "/__fauxcall"
DEMO = {"X-API-Key": "demo-key"}


def _client(**settings: object) -> httpx2.AsyncClient:
    options: dict[str, object] = {"fast": True, "log_level": "WARNING", **settings}
    app = build_app(Settings(**options))  # type: ignore[arg-type]
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://fake", headers=DEMO
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx2.AsyncClient]:
    async with _client() as c:
        yield c


async def _freeze(client: httpx2.AsyncClient, at: str) -> None:
    response = await client.post(f"{ADMIN}/clock", json={"op": "freeze", "at": at})
    assert response.status_code == 200


async def _advance(client: httpx2.AsyncClient, seconds: float) -> None:
    response = await client.post(f"{ADMIN}/clock", json={"op": "advance", "seconds": seconds})
    assert response.status_code == 200


# -- parsing helpers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5", 5),
        (" 5", 5),
        ("+5", 5),
        ("05", 5),
        ("1_001", 1001),
        ("1001.0", 1001),
        ("-1", -1),
        ("", None),
        ("2.5", None),
        ("1e3", None),
        ("inf", None),
        ("abc", None),
    ],
)
def test_lax_int(text: str, expected: int | None) -> None:
    assert handlers.lax_int(text) == expected


def test_cursor_round_trip_and_tolerance() -> None:
    cursor = handlers.encode_cursor("AAPL")
    assert cursor == "eyJhZnRlciI6IkFBUEwifQ"
    assert handlers.decode_cursor(cursor) == "AAPL"
    assert handlers.decode_cursor(cursor + "==") == "AAPL"
    spaced = base64.b64encode(b'{"after": "9988.HK", "x": 1}').decode()
    assert handlers.decode_cursor(spaced) == "9988.HK"
    for bad in ("garbage!!", "aGVsbG8", handlers.encode_cursor(5)):  # type: ignore[arg-type]
        assert handlers.decode_cursor(bad) is None
    as_list = base64.urlsafe_b64encode(b"[1]").decode()
    assert handlers.decode_cursor(as_list) is None


def test_time_formats() -> None:
    ns = 1_791_698_662_731_818_000
    assert handlers.iso_ms_z(ns) == "2026-10-11T06:04:22.731Z"
    assert handlers.iso_us_z(ns) == "2026-10-11T06:04:22.731818Z"
    assert handlers.python_isoformat(ns) == "2026-10-11T06:04:22.731818+00:00"
    assert handlers.python_isoformat(ns - 731_818_000) == "2026-10-11T06:04:22+00:00"
    assert handlers.parse_isoformat_ns("2026-10-11T06:04:22.731818+00:00") == ns


def test_search_scoring_rules() -> None:
    apple: JsonObject = {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology"}
    shell: JsonObject = {"symbol": "SHEL.L", "name": "Shell plc", "sector": "Energy"}
    assert handlers.score("aapl", apple) == Decimal("1.0")
    assert handlers.score("aa", apple) == Decimal("0.9")
    assert handlers.score("apple", apple) == Decimal("0.8")
    assert handlers.score("app", apple) == Decimal("0.6")
    assert handlers.score("ap", apple) == 0
    assert handlers.score("cloud", apple) == Decimal("0.5")
    assert handlers.score("apple apple bank", apple) == Decimal("0.533")
    assert handlers.score("shel", shell) == Decimal("1.0")
    assert handlers.score("!!", apple) == 0
    assert handlers.tokens("LVMH Moët") == ["lvmh", "mo", "t"]


def test_ascii_filename_and_allowed_methods() -> None:
    assert handlers.ascii_filename("résumé données.csv") == "r_sum_ donn_es.csv"
    templates = [("GET", "/v1/watchlists"), ("POST", "/v1/watchlists"), ("GET", "/v1/w/{id}")]
    assert handlers.allowed_methods(templates, "/v1/watchlists") == ["GET", "POST"]
    assert handlers.allowed_methods(templates, "/v1/w/12") == ["GET"]
    assert handlers.allowed_methods(templates, "/v1/w/12/x") == []


def test_trade_walks_missing_file(tmp_path: Path) -> None:
    assert handlers.trade_walks(tmp_path) == {}


# -- reads -------------------------------------------------------------------------------------


async def test_tickers_pagination_walk(client: httpx2.AsyncClient) -> None:
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params = {"limit": "7", **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/v1/tickers", params=params)).json()
        assert page["total"] == 60
        seen += [t["symbol"] for t in page["data"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(seen)
    assert len(seen) == 60


async def test_tickers_errors_in_real_order(client: httpx2.AsyncClient) -> None:
    async def code(**params: str) -> tuple[int, str, str | None]:
        r = await client.get("/v1/tickers", params=params)
        error = r.json()["error"]
        return r.status_code, error["code"], error.get("param")

    assert await code(limit="0", exchange="XXXX") == (400, "invalid_parameter", "limit")
    assert await code(exchange="XXXX", sector="Nope") == (400, "invalid_parameter", "exchange")
    assert await code(sector="Nope", cursor="x") == (400, "invalid_parameter", "sector")
    assert await code(cursor=handlers.encode_cursor("ZZZZ")) == (400, "invalid_cursor", "cursor")


async def test_ticker_cursor_on_filtered_out_symbol(client: httpx2.AsyncClient) -> None:
    params = {"exchange": "XNYS", "cursor": handlers.encode_cursor("aapl"), "limit": "3"}
    page = (await client.get("/v1/tickers", params=params)).json()
    assert [t["symbol"] for t in page["data"]] == ["BA", "BAC", "CAT"]
    assert page["total"] == 16


async def test_quote_follows_the_exchange_calendar(client: httpx2.AsyncClient) -> None:
    await _freeze(client, "2026-10-12T14:00:00Z")  # Monday 10:00 New York
    open_quote = (await client.get("/v1/tickers/aapl/quote")).json()
    assert open_quote["market_state"] == "open"
    assert open_quote["as_of"] == "2026-10-12T13:59:58.500000Z"
    await _freeze(client, "2026-10-12T02:45:00Z")  # Tokyo lunch break (11:45 JST)
    lunch = (await client.get("/v1/tickers/7203.T/quote")).json()
    assert lunch["market_state"] == "closed"
    assert lunch["as_of"] == "2026-10-12T02:30:00.000000Z"
    missing = await client.get("/v1/tickers/zzzz/quote")
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == "No ticker with symbol 'ZZZZ'."


async def test_time_tracks_the_fake_clock(client: httpx2.AsyncClient) -> None:
    await _freeze(client, "2026-10-11T06:17:28.326675Z")
    body = (await client.get("/v1/time")).json()
    assert body == {"utc": "2026-10-11T06:17:28.3266750Z", "unix_ms": 1791699448326, "zone": "UTC"}


async def test_search_validation(client: httpx2.AsyncClient) -> None:
    missing = await client.get("/v1/search")
    assert missing.json()["error"]["code"] == "missing_parameter"
    short = await client.get("/v1/search", params={"q": "a"})
    assert short.json()["error"]["param"] == "q"
    padded = (await client.get("/v1/search", params={"q": " a "})).json()
    assert padded["results"][0]["symbol"] == "GOOGL"


# -- watchlists --------------------------------------------------------------------------------


async def test_watchlist_lifecycle(client: httpx2.AsyncClient) -> None:
    created = await client.post(
        "/v1/watchlists", json={"name": "w", "symbols": ["aapl", "MSFT", "AAPL"]}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["id"] == 1001
    assert body["symbols"] == ["AAPL", "MSFT"]
    assert created.headers["location"] == "/v1/watchlists/1001"
    assert (await client.get("/v1/watchlists/1001.0")).json()["name"] == "w"
    put = await client.put("/v1/watchlists/1001", json={"name": "p", "symbols": ["tsla"]})
    assert put.json()["symbols"] == ["TSLA"]
    patched = await client.patch("/v1/watchlists/+1001", json={"color": "red"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "p"
    assert [w["id"] for w in (await client.get("/v1/watchlists")).json()] == [1001]
    assert (await client.delete("/v1/watchlists/1001")).status_code == 204
    again = await client.delete("/v1/watchlists/1001")
    assert again.json()["error"]["code"] == "watchlist_not_found"
    second = await client.post("/v1/watchlists", json={"name": "x"})
    assert second.json()["id"] == 1002  # ids are never reused


async def test_watchlist_validation(client: httpx2.AsyncClient) -> None:
    async def error(method: str, path: str, **kwargs: object) -> tuple[int, str, str | None]:
        r = await client.request(method, path, **kwargs)  # type: ignore[arg-type]
        found = r.json()["error"]
        return r.status_code, found["code"], found.get("param")

    post = ("POST", "/v1/watchlists")
    assert await error(*post, json=[1]) == (400, "invalid_body", None)
    assert await error(*post, content=b"{", headers={"content-type": "application/json"}) == (
        400,
        "invalid_json",
        None,
    )
    assert await error(*post, json={"symbols": "x"}) == (400, "missing_parameter", "name")
    assert await error(*post, json={"name": ""}) == (400, "invalid_parameter", "name")
    assert await error(*post, json={"name": "x", "symbols": [1]}) == (
        400,
        "invalid_parameter",
        "symbols",
    )
    assert await error(*post, json={"name": "x", "symbols": ["YYYY", "ZZZZ"]}) == (
        400,
        "unknown_symbol",
        "symbols",
    )
    assert await error("PUT", "/v1/watchlists/abc", json={"name": ""}) == (
        400,
        "invalid_parameter",
        "id",
    )
    assert await error("PUT", "/v1/watchlists/999999", json={"name": "x"}) == (
        400,
        "missing_parameter",
        "symbols",
    )
    assert await error("PATCH", "/v1/watchlists/999999", json={"name": None}) == (
        400,
        "invalid_parameter",
        "name",
    )
    assert await error("PATCH", "/v1/watchlists/999999", json={}) == (
        404,
        "watchlist_not_found",
        None,
    )
    assert await error("GET", "/v1/watchlists/1e3") == (400, "invalid_parameter", "id")
    assert await error("DELETE", "/v1/watchlists/abc") == (400, "invalid_parameter", "id")


async def test_read_only_key_cannot_write(client: httpx2.AsyncClient) -> None:
    readonly = {"X-API-Key": "readonly-key"}
    for method, path in [
        ("POST", "/v1/watchlists"),
        ("PATCH", "/v1/watchlists/1"),
        ("DELETE", "/v1/watchlists/1"),
        ("POST", "/v1/uploads"),
        ("DELETE", "/v1/uploads/x"),
    ]:
        r = await client.request(method, path, headers=readonly)
        assert r.status_code == 403
        assert r.json()["error"]["message"] == (
            f"This API key is read-only; {method} requests are not allowed."
        )
    assert (await client.get("/v1/watchlists", headers=readonly)).status_code == 200


# -- uploads -----------------------------------------------------------------------------------


async def _upload(client: httpx2.AsyncClient, content: bytes, **data: str) -> dict[str, object]:
    files = {"file": ("prices.csv", content, "text/csv")}
    response = await client.post("/v1/uploads", files=files, data=data)
    assert response.status_code == 202
    body: dict[str, object] = response.json()
    return body


async def test_upload_lifecycle(client: httpx2.AsyncClient) -> None:
    await _freeze(client, "2026-10-11T06:24:11.465149Z")
    job = await _upload(client, b"a,b\n1,2\n", label="daily")
    assert job["status"] == "queued"
    assert job["label"] == "daily"
    assert job["created_at"] == "2026-10-11T06:24:11.465149+00:00"
    url = f"/v1/uploads/{job['id']}"
    early = await client.get(f"{url}/content")
    assert early.status_code == 409
    assert "is queued;" in early.json()["error"]["message"]
    await _advance(client, 3)
    processing = (await client.get(url)).json()
    assert (processing["status"], processing["progress"]) == ("processing", 16)
    await _advance(client, 5)
    done = (await client.get(url)).json()
    assert done["status"] == "done"
    assert done["completed_at"] == "2026-10-11T06:24:19.465149+00:00"
    content = await client.get(f"{url}/content")
    assert content.content == b"a,b\n1,2\n"
    assert content.headers["content-type"] == "text/csv"
    assert content.headers["content-disposition"] == 'attachment; filename="prices.csv"'
    assert (await client.delete(url)).status_code == 204
    assert (await client.get(f"{url}/content")).status_code == 404
    assert (await client.delete(url)).status_code == 404


async def test_corrupt_upload_fails_and_list_is_newest_first(
    client: httpx2.AsyncClient,
) -> None:
    await _freeze(client, "2026-10-11T06:00:00Z")
    first = await _upload(client, b"CORRUPT", label="")
    assert first["label"] is None
    await _advance(client, 1)
    second = await _upload(client, b"corrupt is fine")
    await _advance(client, 10)
    listing = (await client.get("/v1/uploads")).json()
    assert [u["id"] for u in listing] == [second["id"], first["id"]]
    assert listing[1]["status"] == "failed"
    assert listing[1]["error"] == "corrupt_file"
    assert listing[0]["status"] == "done"
    failed = await client.get(f"/v1/uploads/{first['id']}/content")
    assert failed.status_code == 409
    upper = await client.get(f"/v1/uploads/{str(first['id']).upper()}")
    assert upper.status_code == 404


async def test_upload_validation(client: httpx2.AsyncClient) -> None:
    missing = await client.post("/v1/uploads", data={"label": "x"})
    assert missing.json()["error"]["code"] == "missing_parameter"
    text = await client.post("/v1/uploads", data={"file": "just text"})
    assert text.json()["error"]["message"] == (
        "Invalid value for parameter 'file': must be a file."
    )
    big = await client.post("/v1/uploads", files={"file": ("b", b"a" * 1_048_577)})
    assert big.status_code == 413
    two = await client.post(
        "/v1/uploads",
        files=[("file", ("first.txt", b"first")), ("file", ("second.txt", b"second!"))],
    )
    assert two.json()["filename"] == "second.txt"
    assert two.json()["size"] == 7


# -- stream ------------------------------------------------------------------------------------


async def test_trade_stream_replays_the_walk(client: httpx2.AsyncClient) -> None:
    response = await client.get("/v1/stream/trades", params={"symbol": "aapl", "count": "3"})
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    frames = [f for f in response.text.split("\n\n") if f]
    assert len(frames) == 3
    first = dict(line.split(": ", 1) for line in frames[0].split("\n"))
    assert first["id"] == "1"
    assert first["event"] == "trade"
    data = json.loads(first["data"])
    assert (data["symbol"], data["price"], data["size"]) == ("AAPL", "227.50", 189)


async def test_trade_stream_validation(client: httpx2.AsyncClient) -> None:
    async def error(**params: str) -> tuple[str, str]:
        found = (await client.get("/v1/stream/trades", params=params)).json()["error"]
        return found["code"], found["param"]

    assert await error() == ("missing_parameter", "symbol")
    assert await error(symbol="ZZZZ", count="0") == ("invalid_parameter", "count")
    assert await error(symbol="") == ("unknown_symbol", "symbol")


async def test_trade_stream_beyond_recorded_walk(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "trade_walks", lambda _data_dir: {"AAPL": [("1.00", 5)]})
    response = await client.get("/v1/stream/trades", params={"symbol": "AAPL", "count": "2"})
    prices = [json.loads(f.split("data: ", 1)[1])["price"] for f in response.text.split("\n\n")[:2]]
    assert prices == ["1.00", "227.48"]


# -- routing, auth and limits ------------------------------------------------------------------


async def test_unknown_routes_and_methods(client: httpx2.AsyncClient) -> None:
    no_key = await client.get("/v1/nonexistent", headers={"X-API-Key": ""})
    assert no_key.status_code == 401
    unknown = await client.get("/v1/exchanges/")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["message"] == "No route matches GET /v1/exchanges/."
    wrong = await client.put("/v1/watchlists", json={})
    assert wrong.status_code == 405
    assert wrong.headers["allow"] == "GET, POST"
    health = await client.post("/healthz")
    assert health.status_code == 405
    assert "x-ratelimit-limit" not in health.headers
    root = await client.get("/nope")
    assert root.json()["error"]["code"] == "not_found"


async def test_auth_outcomes(client: httpx2.AsyncClient) -> None:
    revoked = await client.get("/v1/exchanges", headers={"X-API-Key": "revoked-key"})
    assert revoked.json()["error"]["code"] == "api_key_revoked"
    assert "x-ratelimit-limit" not in revoked.headers
    invalid = await client.get("/v1/exchanges", headers={"X-API-Key": "nope"})
    assert invalid.json()["error"]["code"] == "invalid_api_key"
    assert (await client.get("/healthz", headers={"X-API-Key": ""})).status_code == 200


async def test_rate_limit_per_key() -> None:
    async with _client(fast=False, latency=False) as client:
        unknown = await client.get("/v1/exchanges/")
        assert unknown.headers["x-ratelimit-remaining"] == "29"  # unknown /v1 routes count too
        statuses = [(await client.get("/v1/time")).status_code for _ in range(30)]
        assert statuses[:29] == [200] * 29
        assert statuses[29] == 429
        limited = await client.get("/v1/nonexistent")
        assert limited.status_code == 429
        assert 1 <= int(limited.headers["retry-after"]) <= 10
        assert limited.json()["error"]["code"] == "rate_limited"
        assert limited.json()["error"]["message"] == (
            "Rate limit exceeded: 30 requests per 10 seconds."  # observed in review, see notes
        )
        bare = await client.get("/v1")  # bare /v1 is a /v1 route: counted, so limited too
        assert bare.status_code == 429
        readonly = {"X-API-Key": "readonly-key"}
        other = await client.get("/v1/time", headers=readonly)
        assert other.status_code == 200
        for _ in range(29):
            await client.get("/v1/time", headers=readonly)
        write = await client.post("/v1/watchlists", json={"name": "x"}, headers=readonly)
        assert write.status_code == 429  # the limit applies before the read-only check


async def test_bare_v1_authenticates_first(client: httpx2.AsyncClient) -> None:
    missing = await client.get("/v1", headers={"X-API-Key": ""})
    assert missing.status_code == 401
    known = await client.get("/v1")
    assert known.status_code == 404
    assert known.json()["error"]["code"] == "not_found"


def test_quote_currency_invariant() -> None:
    app = build_app(Settings(fast=True, log_level="WARNING"))
    runtime = app.state.fauxcall
    world = runtime.world.session("default")
    assert handlers.quote_currency_invariant(world) == []
    world.update("quotes", "AAPL", {"currency": "EUR"})
    assert handlers.quote_currency_invariant(world) == ["AAPL: EUR != USD"]
