"""Reference data: exchanges, tickers (cursor pagination), quotes, search, server time.

Read-only scenarios with inputs the recordings mostly don't have (other page sizes, other
symbols, other queries), so they check the fake's rules rather than its recordings. Every
assertion holds on the real API and on the fake.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fauxcall.testing import CompareOptions

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient

SECTORS = {
    "Automotive",
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Healthcare",
    "Industrials",
    "Semiconductors",
    "Technology",
}


def _after(cursor: str) -> str:
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    return str(payload["after"])


def _cursor(symbol: str) -> str:
    raw = json.dumps({"after": symbol}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


async def _walk(api: ApiClient, **params: Any) -> list[dict[str, Any]]:
    """Every page of a listing, following next_cursor."""
    tickers: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(70):
        query = dict(params) | ({"cursor": cursor} if cursor else {})
        page = (await api.get("/v1/tickers", params=query)).json()
        tickers += page["data"]
        assert page["total"] >= len(tickers)
        cursor = page["next_cursor"]
        if cursor is None:
            assert len(tickers) == page["total"]
            return tickers
        assert _after(cursor) == page["data"][-1]["symbol"]
    raise AssertionError("the walk never ended")


async def test_exchanges_list_matches_each_detail(api: ApiClient) -> None:
    exchanges = (await api.get("/v1/exchanges")).json()
    codes = [exchange["code"] for exchange in exchanges]
    assert codes == sorted(codes)
    for exchange in exchanges:
        spelled = (
            exchange["code"].lower() if exchange["code"] in {"XLON", "XHKG"} else exchange["code"]
        )
        detail = await api.get(f"/v1/exchanges/{spelled}")
        assert detail.json() == exchange
        assert all(len(session["open"]) == 5 for session in exchange["sessions"])


async def test_walk_every_ticker_with_an_odd_page_size(api: ApiClient) -> None:
    tickers = await _walk(api, limit=17)
    symbols = [ticker["symbol"] for ticker in tickers]
    assert symbols == sorted(symbols)
    assert len(set(symbols)) == len(symbols) == 60
    assert {ticker["sector"] for ticker in tickers} == SECTORS


async def test_every_ticker_references_a_listed_exchange(api: ApiClient) -> None:
    exchanges = {e["code"] for e in (await api.get("/v1/exchanges")).json()}
    tickers = (await api.get("/v1/tickers", params={"limit": 100})).json()["data"]
    assert {ticker["exchange"] for ticker in tickers} == exchanges  # each exchange lists tickers
    for code in sorted(exchanges):
        page = (await api.get("/v1/tickers", params={"exchange": code.lower(), "limit": 3})).json()
        assert all(ticker["exchange"] == code for ticker in page["data"])
        assert page["total"] == sum(1 for t in tickers if t["exchange"] == code)


async def test_filtered_walks(api: ApiClient) -> None:
    autos = await _walk(api, exchange="xtks", sector="automotive", limit=1)
    assert [t["symbol"] for t in autos] == ["7203.T", "7267.T"]
    staples = await _walk(api, sector="CONSUMER STAPLES", limit=3)
    assert all(t["sector"] == "Consumer Staples" for t in staples)
    london = await _walk(api, exchange="XLON", limit=2)
    assert all(t["currency"] == "GBP" for t in london)


async def test_cursor_from_one_listing_in_another(api: ApiClient) -> None:
    page = (await api.get("/v1/tickers", params={"limit": 25})).json()
    resumed = await api.get(
        "/v1/tickers", params={"cursor": page["next_cursor"], "exchange": "XNYS", "limit": 4}
    )
    assert all(t["symbol"] > _after(page["next_cursor"]) for t in resumed.json()["data"])
    lowered = await api.get("/v1/tickers", params={"cursor": _cursor("msft"), "limit": 2})
    assert [t["symbol"] for t in lowered.json()["data"]] == ["NFLX", "NVDA"]
    last = await api.get("/v1/tickers", params={"cursor": _cursor("WFC")})
    assert [t["symbol"] for t in last.json()["data"]] == ["XOM"]


async def test_repeated_and_empty_parameters(api: ApiClient) -> None:
    repeated = await api.get("/v1/tickers", params=[("sector", "Energy"), ("sector", "Healthcare")])
    assert {t["sector"] for t in repeated.json()["data"]} == {"Healthcare"}
    ignored = await api.get("/v1/tickers", params={"exchange": "", "sector": "", "cursor": ""})
    assert ignored.json()["total"] == 60
    spelled = await api.get("/v1/tickers", params={"limit": " 7"})
    assert len(spelled.json()["data"]) == 7


async def test_ticker_detail_in_any_case(api: ApiClient) -> None:
    for symbol in ["nflx", "Ulvr.l", "9984.t", "TTE.PA", "0388.hk"]:
        ticker = (await api.get(f"/v1/tickers/{symbol}")).json()
        assert ticker["symbol"] == symbol.upper()
        assert datetime.strptime(ticker["listed_on"], "%Y-%m-%d").replace(tzinfo=UTC)


async def test_quotes_follow_their_exchange(api: ApiClient) -> None:
    for symbol in ["JPM", "QCOM", "ULVR.L", "TTE.PA", "0388.HK", "8306.T"]:
        ticker = (await api.get(f"/v1/tickers/{symbol}")).json()
        quote = (await api.get(f"/v1/tickers/{symbol.lower()}/quote")).json()
        assert quote["symbol"] == symbol
        assert quote["currency"] == ticker["currency"]
        assert quote["market_state"] in {"open", "closed"}
        whole, cents = quote["price"].split(".")
        assert whole.isdigit()
        assert len(cents) == 2
        as_of = datetime.fromisoformat(quote["as_of"])
        assert as_of <= datetime.now(UTC)
        assert quote["as_of"].endswith("Z")
        assert len(quote["as_of"]) == 27


async def test_quotes_are_stable(api: ApiClient) -> None:
    first = (await api.get("/v1/tickers/V/quote")).json()
    second = (await api.get("/v1/tickers/v/quote")).json()
    assert first["price"] == second["price"]
    assert first["market_state"] == second["market_state"]


async def test_search_queries(api: ApiClient) -> None:
    for query in [
        "microsoft",
        "goldman",
        "walt",
        "pay",
        "toyota honda",
        "bank of america",
        "rolls royce",
        "airbus boeing",
        "coca",
        "PEP",
        "netflix streaming",
        "oil & gas",
        "semis tsm",
        "Volkswagen",
        "co",
    ]:
        body = (await api.get("/v1/search", params={"q": query})).json()
        assert body["query"] == query
        scores = [hit["score"] for hit in body["results"]]
        assert scores == sorted(scores, reverse=True)
        assert len(scores) <= 10


async def test_search_results_resolve(api: ApiClient) -> None:
    results = (await api.get("/v1/search", params={"q": "holdings group"})).json()["results"]
    assert results
    for hit in results[:4]:
        ticker = (await api.get(f"/v1/tickers/{hit['symbol']}")).json()
        assert ticker["name"] == hit["name"]


async def test_search_bounds(api: ApiClient) -> None:
    exact = await api.get("/v1/search", params={"q": "ab" * 32})
    assert exact.status_code == 200
    blank = await api.get("/v1/search", params={"q": "  "})
    assert blank.json()["results"] == []


async def test_server_time_is_now(api: ApiClient) -> None:
    body = (await api.get("/v1/time")).json()
    utc = datetime.strptime(body["utc"][:26] + "+0000", "%Y-%m-%dT%H:%M:%S.%f%z")
    assert abs(utc.timestamp() - datetime.now(UTC).timestamp()) < 30
    assert abs(body["unix_ms"] - int(utc.timestamp() * 1000)) <= 1
    assert body["zone"] == "UTC"
    assert body["utc"].endswith("0Z")
    assert len(body["utc"]) == 28


async def test_spec_document(api: ApiClient) -> None:
    """The REST spec, as the real API serves it (values: the real side's copy goes through the
    recording privacy filter, which blanks credential-looking fields, so compare shape here)."""
    spec = (await api.get("/openapi.json", compare=CompareOptions(values=False))).json()
    assert spec["info"]["title"] == "FauxCall Reference Market API"
    assert len(spec["paths"]) == 14
    assert "servers" not in spec
    assert "/v1/watchlists/{id}" in spec["paths"]


async def test_health(api: ApiClient) -> None:
    assert (await api.get("/healthz")).json() == {"status": "ok"}
    keyless = await api.get("/healthz", headers={"X-API-Key": "not-a-key"})
    assert keyless.status_code == 200
