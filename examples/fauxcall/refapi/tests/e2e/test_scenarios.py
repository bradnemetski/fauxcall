"""End-to-end scenarios: each runs against the fake, the real API, or both (``--target=parity``).

Assertions hold on both sides (behavior, not recorded values). Writes to the real API need
``--allow-real-writes``; every scenario that creates something deletes it in ``finally``.
Several scenarios deliberately use inputs that were never recorded (search queries, limit
spellings, symbols) so parity measures how well the fake generalizes, not just replays.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import pytest
from fauxcall.model.fields import RandomPolicy, VolatilePolicy
from fauxcall.testing import CompareOptions
from market import exchanges_by_code, get_quote

if TYPE_CHECKING:
    import httpx2
    from fauxcall.testing import ApiClient

REQUEST_ID: Final = RandomPolicy(
    kind="random", scope="body", path="error.request_id", generator="uuid4"
)
UNROUTED: Final = CompareOptions(policies=[REQUEST_ID])
"""Unknown routes map to no endpoint: give their error bodies the endpoints' request_id policy."""
ID_IN_MESSAGE: Final = CompareOptions(
    policies=[REQUEST_ID, VolatilePolicy(kind="volatile", scope="body", path="error.message")]
)
"""Errors whose message names a server-assigned id (a watchlist counter, an upload UUID): the ids
differ between the two servers by design, so the scenario asserts the message format itself."""
EXCHANGES: Final = ["XHKG", "XLON", "XNAS", "XNYS", "XPAR", "XTKS"]
REVOKED: Final = {"X-API-Key": "revoked-key"}
READONLY: Final = {"X-API-Key": "readonly-key"}


def cursor(symbol: Any) -> str:
    raw = json.dumps({"after": symbol}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def error_of(response: httpx2.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    assert set(error) <= {"code", "message", "request_id", "param"}
    return error


# -- health, exchanges -------------------------------------------------------------------------


async def test_healthz_needs_no_key(api: ApiClient) -> None:
    assert (await api.get("/healthz")).json() == {"status": "ok"}
    assert (await api.get("/healthz", headers={"X-API-Key": "nope"})).status_code == 200


async def test_exchanges_list_matches_items(api: ApiClient) -> None:
    listing = (await api.get("/v1/exchanges")).json()
    assert [e["code"] for e in listing] == EXCHANGES
    for exchange in listing:
        item = (await api.get(f"/v1/exchanges/{exchange['code'].lower()}")).json()
        assert item == exchange


async def test_exchanges_ignore_query_parameters(api: ApiClient) -> None:
    response = await api.get("/v1/exchanges", params={"foo": "bar", "limit": "1"})
    assert len(response.json()) == 6


@pytest.mark.parametrize("code", ["XXXX", "123", "x", "XNAS ", "xnys1"])
async def test_exchange_not_found(api: ApiClient, code: str) -> None:
    response = await api.get(f"/v1/exchanges/{code}")
    assert response.status_code == 404
    assert error_of(response)["message"] == f"No exchange with code '{code.upper()}'."


# -- tickers -----------------------------------------------------------------------------------


async def test_tickers_default_page(api: ApiClient) -> None:
    page = (await api.get("/v1/tickers")).json()
    assert len(page["data"]) == 20
    assert page["total"] == 60
    assert page["next_cursor"] == cursor(page["data"][-1]["symbol"])


async def test_tickers_pagination_walk(api: ApiClient) -> None:
    symbols: list[str] = []
    next_cursor: str | None = None
    for _ in range(10):
        params = {"limit": "13", **({"cursor": next_cursor} if next_cursor else {})}
        page = (await api.get("/v1/tickers", params=params)).json()
        symbols += [t["symbol"] for t in page["data"]]
        next_cursor = page["next_cursor"]
        if next_cursor is None:
            break
    assert len(symbols) == 60
    assert symbols == sorted(symbols)


@pytest.mark.parametrize("exchange", [*EXCHANGES, "xtks"])
async def test_tickers_by_exchange_reference_real_exchanges(api: ApiClient, exchange: str) -> None:
    page = (await api.get("/v1/tickers", params={"exchange": exchange, "limit": "100"})).json()
    assert page["total"] == len(page["data"]) > 0
    assert {t["exchange"] for t in page["data"]} == {exchange.upper()}
    venue = (await api.get(f"/v1/exchanges/{exchange}")).json()
    assert {t["currency"] for t in page["data"]} == {venue["currency"]}


@pytest.mark.parametrize(
    "sector", ["Technology", "financials", "CONSUMER STAPLES", "Semiconductors", "energy"]
)
async def test_tickers_by_sector(api: ApiClient, sector: str) -> None:
    page = (await api.get("/v1/tickers", params={"sector": sector, "limit": "100"})).json()
    assert {t["sector"].lower() for t in page["data"]} == {sector.lower()}


async def test_tickers_combined_filters_paginate(api: ApiClient) -> None:
    params = {"exchange": "XNYS", "sector": "Financials", "limit": "2"}
    first = (await api.get("/v1/tickers", params=params)).json()
    second = (
        await api.get("/v1/tickers", params={**params, "cursor": first["next_cursor"]})
    ).json()
    assert first["total"] == second["total"]
    assert not {t["symbol"] for t in first["data"]} & {t["symbol"] for t in second["data"]}


async def test_tickers_filters_that_match_nothing(api: ApiClient) -> None:
    page = (
        await api.get("/v1/tickers", params={"exchange": "XPAR", "sector": "Automotive"})
    ).json()
    assert page == {"data": [], "next_cursor": None, "total": 0}


@pytest.mark.parametrize(
    ("query", "count"),
    [
        ("exchange=&sector=&cursor=", 20),  # empty filters are ignored
        ("exchange=XNAS&exchange=XLON", 7),  # the last repeated value wins
        ("limit=2&limit=4", 4),
        ("limit=+5", 5),  # limit is parsed leniently (000087-000089)
        ("limit=05", 5),
        ("limit=5.0", 5),
        ("limit=%205%20", 5),
        ("limit=1", 1),
        ("limit=100", 60),
        ("foo=bar", 20),
    ],
)
async def test_tickers_parameter_spellings(api: ApiClient, query: str, count: int) -> None:
    response = await api.get(f"/v1/tickers?{query}")
    assert response.status_code == 200
    assert len(response.json()["data"]) == count


@pytest.mark.parametrize(
    ("query", "param"),
    [
        ("limit=0", "limit"),
        ("limit=101", "limit"),
        ("limit=-3", "limit"),
        ("limit=ten", "limit"),
        ("limit=", "limit"),
        ("limit=2.5", "limit"),
        ("limit=true", "limit"),
        ("exchange=NYSE", "exchange"),
        ("exchange=%20XNAS", "exchange"),
        ("sector=Tech", "sector"),
        ("sector=Financial", "sector"),
        ("cursor=not-a-cursor", "cursor"),
        (f"cursor={cursor('NOPE')}", "cursor"),
        (f"cursor={cursor(None)}", "cursor"),
        (f"cursor={cursor(7)}", "cursor"),
        ("limit=0&exchange=NYSE&sector=Tech&cursor=x", "limit"),
        ("exchange=NYSE&sector=Tech&cursor=x", "exchange"),
        ("sector=Tech&cursor=x", "sector"),
    ],
)
async def test_tickers_invalid_parameters(api: ApiClient, query: str, param: str) -> None:
    response = await api.get(f"/v1/tickers?{query}")
    assert response.status_code == 400
    assert error_of(response)["param"] == param


@pytest.mark.parametrize("after", ["aapl", "AZN.L", "XOM", "0005.HK", "msft"])
async def test_tickers_handmade_cursors(api: ApiClient, after: str) -> None:
    page = (await api.get("/v1/tickers", params={"cursor": cursor(after), "limit": "5"})).json()
    assert all(t["symbol"] > after.upper() for t in page["data"])


async def test_ticker_cursor_from_another_filter(api: ApiClient) -> None:
    params = {"exchange": "XLON", "cursor": cursor("AMZN"), "limit": "3"}
    page = (await api.get("/v1/tickers", params=params)).json()
    assert page["total"] == 7


async def test_every_listed_ticker_resolves(api: ApiClient) -> None:
    page = (await api.get("/v1/tickers", params={"limit": "100"})).json()
    for ticker in page["data"][::4]:
        detail = (await api.get(f"/v1/tickers/{ticker['symbol'].lower()}")).json()
        assert detail == ticker


@pytest.mark.parametrize("symbol", ["ZZZZ", "brk.b", "A$B", "A B", "AAPL.", "1234", "É", "TSLA-X"])
async def test_ticker_not_found(api: ApiClient, symbol: str) -> None:
    response = await api.get(f"/v1/tickers/{symbol}")
    assert response.status_code == 404
    assert error_of(response)["code"] == "ticker_not_found"


# -- quotes ------------------------------------------------------------------------------------


async def test_quotes_are_consistent_with_tickers(api: ApiClient) -> None:
    exchanges = await exchanges_by_code(api)
    page = (await api.get("/v1/tickers", params={"limit": "100"})).json()
    for ticker in page["data"][::3]:
        quote = await get_quote(api, ticker["symbol"], exchanges[ticker["exchange"]])
        assert quote["symbol"] == ticker["symbol"]
        assert quote["currency"] == ticker["currency"]
        assert len(quote["price"].split(".")[1]) == 2


async def test_quote_is_static_and_case_insensitive(api: ApiClient) -> None:
    first = (await api.get("/v1/tickers/nvda/quote")).json()
    second = (await api.get("/v1/tickers/NVDA/quote")).json()
    assert first["price"] == second["price"]
    assert first["symbol"] == "NVDA"


async def test_quote_unknown_symbol(api: ApiClient) -> None:
    response = await api.get("/v1/tickers/nope/quote")
    assert error_of(response)["message"] == "No ticker with symbol 'NOPE'."


# -- time --------------------------------------------------------------------------------------


async def test_server_time_is_now(api: ApiClient) -> None:
    before = time.time()
    body = (await api.get("/v1/time")).json()
    assert body["zone"] == "UTC"
    assert abs(body["unix_ms"] / 1000 - before) < 30
    assert len(body["utc"].split(".")[1]) == 8  # 7 digits + Z
    assert body["utc"][-2] == "0"  # the 7th fractional digit is always 0
    utc_ms = datetime.fromisoformat(body["utc"][:-3] + "+00:00").timestamp() * 1000
    assert abs(utc_ms - body["unix_ms"]) < 1
    later = (await api.get("/v1/time", params={"zone": "Asia/Tokyo"})).json()
    assert later["unix_ms"] >= body["unix_ms"]


# -- search ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "apple",
        "AAPL",
        "microsoft corporation",
        "bank of america",
        "tesla motors",
        "amazon inc",
        "visa",
        "oil gas",
        "semi conductor",
        "chips",
        "nflx",
        "goo",
        "co",
        "inc.",
        "japan tech",
        "the coca-cola company",
        "Holdings plc",
        "group inc",
        "ford",
        "general",
        "mitsubishi ufj",
        "rolls",
        "software cloud",
        "telecom media",
        "zz",
        "ab",
        "moët hennessy",
        "  spaced  out  ",
        "123",
        "0700",
        "luxury retail ecommerce",
        "pharma biotech drugs",
        "exchange clearing",
        "x" * 64,
    ],
)
async def test_search_queries(api: ApiClient, query: str) -> None:
    body = (await api.get("/v1/search", params={"q": query})).json()
    assert body["query"] == query
    scores = [r["score"] for r in body["results"]]
    assert scores == sorted(scores, reverse=True)
    assert len(scores) <= 10


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "a"}, {"q": "x" * 65}])
async def test_search_invalid(api: ApiClient, params: dict[str, str]) -> None:
    response = await api.get("/v1/search", params=params)
    assert response.status_code == 400
    assert error_of(response)["param"] == "q"


async def test_search_ranking_examples(api: ApiClient) -> None:
    apple = (await api.get("/v1/search", params={"q": "apple"})).json()["results"]
    assert (apple[0]["symbol"], apple[0]["score"]) == ("AAPL", 0.8)  # name token exact
    aapl = (await api.get("/v1/search", params={"q": "aapl"})).json()["results"]
    assert (aapl[0]["symbol"], aapl[0]["score"]) == ("AAPL", 1.0)  # symbol exact


async def test_search_hits_resolve_to_tickers(api: ApiClient) -> None:
    body = (await api.get("/v1/search", params={"q": "motor"})).json()
    for hit in body["results"]:
        ticker = (await api.get(f"/v1/tickers/{hit['symbol']}")).json()
        assert ticker["name"] == hit["name"]


# -- watchlists --------------------------------------------------------------------------------


async def test_watchlist_lifecycle(api: ApiClient) -> None:
    created = await api.post(
        "/v1/watchlists", json={"name": "fauxcall-e2e", "symbols": ["aapl", "MSFT", "AAPL"]}
    )
    assert created.status_code == 201
    watchlist = created.json()
    path = f"/v1/watchlists/{watchlist['id']}"
    try:
        assert created.headers["location"] == path
        assert watchlist["symbols"] == ["AAPL", "MSFT"]
        assert watchlist["created_at"] == watchlist["updated_at"]
        assert (await api.get(path)).json() == watchlist
        listed = (await api.get("/v1/watchlists")).json()
        assert watchlist["id"] in [w["id"] for w in listed]
        replaced = (
            await api.put(path, json={"name": "fauxcall-e2e-2", "symbols": ["tsla", "7203.t"]})
        ).json()
        assert replaced["symbols"] == ["TSLA", "7203.T"]
        assert replaced["created_at"] == watchlist["created_at"]
        patched = (await api.patch(path, json={"symbols": ["xom"]})).json()
        assert (patched["name"], patched["symbols"]) == ("fauxcall-e2e-2", ["XOM"])
        empty = (await api.patch(path, json={})).json()
        assert empty["symbols"] == ["XOM"]
        for symbol in empty["symbols"]:
            assert (await api.get(f"/v1/tickers/{symbol}")).status_code == 200
    finally:
        assert (await api.delete(path)).status_code == 204
    gone = error_of(await api.get(path, compare=ID_IN_MESSAGE))
    assert gone["message"] == f"No watchlist with id {watchlist['id']}."
    assert error_of(await api.delete(path, compare=ID_IN_MESSAGE))["code"] == (
        "watchlist_not_found"
    )


async def test_watchlist_id_spellings(api: ApiClient) -> None:
    created = (await api.post("/v1/watchlists", json={"name": "fauxcall-e2e-ids"})).json()
    key = created["id"]
    try:
        for spelling in [f"0{key}", f"+{key}", f"{key}.0", f"%20{key}", f"{key}.00"]:
            same = await api.get(f"/v1/watchlists/{spelling}")
            assert (same.status_code, same.json()["id"]) == (200, key)
        for spelling in [f"{key}.5", "1e3", "abc", "nan"]:
            assert (await api.get(f"/v1/watchlists/{spelling}")).status_code == 400
        for spelling in ["-1", "0", "99999999"]:
            assert (await api.get(f"/v1/watchlists/{spelling}")).status_code == 404
    finally:
        await api.delete(f"/v1/watchlists/{key}")


@pytest.mark.parametrize(
    ("body", "code", "param"),
    [
        ({}, "missing_parameter", "name"),
        ({"symbols": ["AAPL"]}, "missing_parameter", "name"),
        ({"name": ""}, "invalid_parameter", "name"),
        ({"name": "n" * 51}, "invalid_parameter", "name"),
        ({"name": None}, "invalid_parameter", "name"),
        ({"name": 7}, "invalid_parameter", "name"),
        ({"name": ["x"]}, "invalid_parameter", "name"),
        ({"name": "x", "symbols": "AAPL"}, "invalid_parameter", "symbols"),
        ({"name": "x", "symbols": [None]}, "invalid_parameter", "symbols"),
        ({"name": "x", "symbols": {"a": 1}}, "invalid_parameter", "symbols"),
        ({"name": "x", "symbols": ["AAPL", "NOPE"]}, "unknown_symbol", "symbols"),
        ({"name": "x", "symbols": ["aapl "]}, "unknown_symbol", "symbols"),
        ({"name": "", "symbols": ["NOPE"]}, "invalid_parameter", "name"),
        ({"name": "x", "symbols": ["NOPE", 1]}, "invalid_parameter", "symbols"),
        ([{"name": "x"}], "invalid_body", None),
        ("just a string", "invalid_body", None),
        (None, "invalid_body", None),
    ],
)
async def test_watchlist_create_validation(
    api: ApiClient,
    body: Any,
    code: str,
    param: str | None,
) -> None:
    response = await api.post("/v1/watchlists", json=body)
    assert response.status_code == 400
    error = error_of(response)
    assert (error["code"], error.get("param")) == (code, param)


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b'{"name": ', "application/json"),
        (b'{"name": "fauxcall-e2e"}', "text/plain"),
        (b"name=fauxcall-e2e", "application/x-www-form-urlencoded"),
        (b"", "application/json"),
    ],
)
async def test_watchlist_create_bad_bodies(
    api: ApiClient, content: bytes, content_type: str
) -> None:
    response = await api.post(
        "/v1/watchlists", content=content, headers={"content-type": content_type}
    )
    assert response.status_code == 400
    assert error_of(response)["code"] in {"invalid_json", "invalid_body"}


async def test_watchlist_changes_validate_before_lookup(api: ApiClient) -> None:
    missing = "/v1/watchlists/99999999"
    assert error_of(await api.put(missing, json={"name": "x"}))["param"] == "symbols"
    assert error_of(await api.put(missing, json={"name": "x", "symbols": []}))["code"] == (
        "watchlist_not_found"
    )
    assert error_of(await api.patch(missing, json={"symbols": None}))["param"] == "symbols"
    assert error_of(await api.patch(missing, json={}))["code"] == "watchlist_not_found"
    assert error_of(await api.put("/v1/watchlists/abc", json={}))["param"] == "id"
    assert error_of(await api.delete("/v1/watchlists/abc"))["param"] == "id"


async def test_read_only_key_cannot_write(api: ApiClient) -> None:
    created = (await api.post("/v1/watchlists", json={"name": "fauxcall-e2e-ro"})).json()
    path = f"/v1/watchlists/{created['id']}"
    try:
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            target = "/v1/watchlists" if method == "POST" else path
            response = await api.request(method, target, json={"name": "x"}, headers=READONLY)
            assert response.status_code == 403
            assert error_of(response)["code"] == "read_only_key"
        assert (await api.get(path, headers=READONLY)).status_code == 200
        upload = await api.post("/v1/uploads", files={"file": ("a.txt", b"a")}, headers=READONLY)
        assert error_of(upload)["code"] == "read_only_key"
    finally:
        await api.delete(path)


# -- uploads -----------------------------------------------------------------------------------


POLL_AT: Final = (4.0, 9.5)
"""Seconds after creation to poll an upload: mid-processing, then just past the 8 s terminal mark.

Fixed checkpoints (not "poll every second until done") keep the call sequence identical on both
sides even though real calls are paced to the rate limit and the fake answers instantly.
"""


async def _wait_terminal(api: ApiClient, path: str, started: float) -> dict[str, Any]:
    states: list[str] = []
    body: dict[str, Any] = {}
    for offset in (*POLL_AT, *(POLL_AT[-1] + i for i in range(1, 20))):
        await asyncio.sleep(max(0.0, started + offset - time.monotonic()))
        body = (await api.get(path)).json()
        states.append(body["status"])
        if body["status"] in {"done", "failed"}:
            break
    assert states[0] == "processing"
    assert states[-1] in {"done", "failed"}
    assert states == sorted(states, key=["queued", "processing", "done", "failed"].index)
    return body


async def test_upload_lifecycle(api: ApiClient) -> None:
    content = b"symbol,price\nAAPL,227.48\n"
    created = await api.post(
        "/v1/uploads",
        files={"file": ("prices.csv", content, "text/csv")},
        data={"label": "fauxcall-e2e"},
    )
    started = time.monotonic()
    assert created.status_code == 202
    job = created.json()
    path = f"/v1/uploads/{job['id']}"
    try:
        assert (job["status"], job["progress"], job["size"]) == ("queued", 0, len(content))
        early = error_of(await api.get(f"{path}/content", compare=ID_IN_MESSAGE))
        assert early["message"] == (
            f"Upload '{job['id']}' is queued; content is available only when status is done."
        )
        done = await _wait_terminal(api, path, started)
        assert done["status"] == "done"
        assert done["created_at"] == job["created_at"]
        download = await api.get(f"{path}/content")
        assert download.content == content
        assert download.headers["content-type"] == "text/csv"
        listed = (await api.get("/v1/uploads")).json()
        assert listed[0]["id"] == job["id"]
    finally:
        assert (await api.delete(path)).status_code == 204
    gone = error_of(await api.get(path, compare=ID_IN_MESSAGE))
    assert gone["message"] == f"No upload with id '{job['id']}'."
    assert (await api.get(f"{path}/content", compare=ID_IN_MESSAGE)).status_code == 404


async def test_corrupt_upload_fails(api: ApiClient) -> None:
    created = await api.post(
        "/v1/uploads", files={"file": ("bad.bin", b"\x00CORRUPT\x01", "application/octet-stream")}
    )
    started = time.monotonic()
    path = f"/v1/uploads/{created.json()['id']}"
    try:
        failed = await _wait_terminal(api, path, started)
        assert (failed["status"], failed["error"]) == ("failed", "corrupt_file")
        content = await api.get(f"{path}/content", compare=ID_IN_MESSAGE)
        assert content.status_code == 409
        assert "is failed;" in error_of(content)["message"]
    finally:
        await api.delete(path)


async def test_upload_details(api: ApiClient) -> None:
    created = await api.post(
        "/v1/uploads",
        files=[
            ("file", ("first.txt", b"first", "text/plain")),
            ("file", ("Ünïcode name.md", b"# corrupt? no, lowercase", "text/markdown")),
        ],
        data={"label": "  padded  "},
    )
    started = time.monotonic()
    job = created.json()
    path = f"/v1/uploads/{job['id']}"
    try:
        assert (job["filename"], job["label"]) == ("Ünïcode name.md", "  padded  ")
        done = await _wait_terminal(api, path, started)
        assert done["status"] == "done"
        download = await api.get(f"{path}/content")
        disposition = download.headers["content-disposition"]
        assert disposition == 'attachment; filename="_n_code name.md"'  # non-ASCII -> _
    finally:
        await api.delete(path)


async def test_upload_validation(api: ApiClient) -> None:
    assert error_of(await api.post("/v1/uploads", data={"label": "x"}))["param"] == "file"
    assert error_of(await api.post("/v1/uploads", data={"file": "text"}))["param"] == "file"
    assert error_of(await api.post("/v1/uploads", json={"file": "x"}))["code"] == (
        "missing_parameter"
    )
    too_big = await api.post("/v1/uploads", files={"file": ("big.txt", b"a" * 1_048_577)})
    assert too_big.status_code == 413
    for path in ["/v1/uploads/abc", "/v1/uploads/00000000-0000-4000-8000-000000000000"]:
        assert (await api.get(path)).status_code == 404
        assert (await api.get(f"{path}/content")).status_code == 404
        assert (await api.delete(path)).status_code == 404


# -- stream ------------------------------------------------------------------------------------


def sse_events(text: str) -> list[dict[str, str]]:
    frames = text.replace("\r\n", "\n").split("\n\n")
    return [
        dict(line.split(": ", 1) for line in frame.split("\n") if ": " in line)
        for frame in frames
        if frame.strip()
    ]


MSFT_WALK: Final = [("415.09", 279), ("415.26", 707), ("415.18", 649), ("415.39", 768)]
"""The first MSFT trades: the walk is deterministic per symbol (documented; recorded 000734 ff.)."""
TRADE_TIME: Final = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")


async def test_trade_stream(api: ApiClient) -> None:
    """Exact events (parity's raw-text check always differs: every event's ``time`` is now)."""
    started = time.time()
    response = await api.get("/v1/stream/trades", params={"symbol": "msft", "count": "4"})
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.text.endswith("\n\n")
    events = sse_events(response.text)
    assert [list(e) for e in events] == [["id", "event", "data"]] * 4
    assert [e["id"] for e in events] == ["1", "2", "3", "4"]
    assert {e["event"] for e in events} == {"trade"}
    data = [json.loads(e["data"]) for e in events]
    assert [list(d) for d in data] == [["symbol", "price", "size", "time"]] * 4
    assert [(d["symbol"], d["price"], d["size"]) for d in data] == [
        ("MSFT", p, s) for p, s in MSFT_WALK
    ]
    times = [d["time"] for d in data]
    assert all(TRADE_TIME.fullmatch(t) for t in times)
    instants = [datetime.fromisoformat(t).timestamp() for t in times]
    assert instants == sorted(instants)
    assert started - 5 < instants[0] < time.time() + 5
    if api.is_real:  # the fake's fast mode skips the 200 ms pauses by design
        gaps = [b - a for a, b in itertools.pairwise(instants)]
        assert all(0.15 < g < 0.35 for g in gaps), gaps
    shorter = await api.get("/v1/stream/trades", params={"symbol": "MSFT", "count": "2"})
    assert [json.loads(e["data"])["price"] for e in sse_events(shorter.text)] == [
        "415.09",
        "415.26",
    ]
    quote = (await api.get("/v1/tickers/MSFT/quote")).json()
    for trade in data:
        assert abs(float(trade["price"]) - float(quote["price"])) < float(quote["price"]) * 0.05


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, "missing_parameter"),
        ({"symbol": "NOPE"}, "unknown_symbol"),
        ({"symbol": ""}, "unknown_symbol"),
        ({"symbol": "AAPL", "count": "0"}, "invalid_parameter"),
        ({"symbol": "AAPL", "count": "51"}, "invalid_parameter"),
        ({"symbol": "AAPL", "count": "x"}, "invalid_parameter"),
        ({"symbol": "NOPE", "count": "0"}, "invalid_parameter"),
        ({"count": "0"}, "missing_parameter"),
    ],
)
async def test_trade_stream_errors(api: ApiClient, params: dict[str, str], code: str) -> None:
    response = await api.get("/v1/stream/trades", params=params)
    assert response.status_code == 400
    assert error_of(response)["code"] == code


# -- auth and routing --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/exchanges", "/v1/time", "/v1/search?q=apple"])
async def test_auth_failures(api: ApiClient, path: str) -> None:
    assert error_of(await api.get(path, headers=REVOKED))["code"] == "api_key_revoked"
    assert error_of(await api.get(path, headers={"X-API-Key": "wrong"}))["code"] == (
        "invalid_api_key"
    )
    assert (await api.get(path, headers=READONLY)).status_code == 200


async def test_empty_api_key_is_missing(api: ApiClient) -> None:
    response = await api.get("/v1/exchanges", headers={"X-API-Key": ""})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    ["/v1/nonexistent", "/V1/Exchanges", "/v1/exchanges/", "/v1/tickers/AAPL/quote/", "/v1"],
)
async def test_unknown_routes(api: ApiClient, path: str) -> None:
    response = await api.get(path, compare=UNROUTED)
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"
    no_key = await api.get(path, headers={"X-API-Key": "wrong"}, compare=UNROUTED)
    # /v1 routes authenticate first (even unknown ones); other prefixes are just unknown
    assert no_key.status_code == (401 if path.split("/")[1] == "v1" else 404)


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("POST", "/v1/exchanges", "GET"),
        ("DELETE", "/v1/tickers/AAPL", "GET"),
        ("PUT", "/v1/watchlists", "GET, POST"),
        ("POST", "/v1/time", "GET"),
        ("PATCH", "/v1/uploads", "GET, POST"),
        ("POST", "/healthz", "GET"),
    ],
)
async def test_methods_not_allowed(api: ApiClient, method: str, path: str, allow: str) -> None:
    response = await api.request(method, path, json={}, compare=UNROUTED)
    assert response.status_code == 405
    assert error_of(response)["code"] == "method_not_allowed"
    assert response.headers["allow"] == allow
    readonly = await api.request(method, path, json={}, headers=READONLY, compare=UNROUTED)
    if path.startswith("/v1/"):  # under /v1 the read-only check runs before routing
        assert (readonly.status_code, error_of(readonly)["code"]) == (403, "read_only_key")
    else:
        assert readonly.status_code == 405


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/v1/nonexistent"), ("PATCH", "/v1"), ("POST", "/v1/ws/quotes"), ("PUT", "/nope")],
)
async def test_read_only_writes_to_unknown_routes(api: ApiClient, method: str, path: str) -> None:
    response = await api.request(method, path, json={}, headers=READONLY, compare=UNROUTED)
    if path.split("/")[1] == "v1":
        assert error_of(response)["message"] == (
            f"This API key is read-only; {method} requests are not allowed."
        )
        assert response.status_code == 403
    else:
        assert response.status_code == 404
