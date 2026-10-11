from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import pytest

from .helpers import DEMO, at, error_of

if TYPE_CHECKING:
    from starlette.testclient import TestClient

    from fauxcall_reference_api.clock import ManualClock


BAD_LIMIT = "Invalid value for parameter 'limit': must be an integer between 1 and 100."
BAD_CURSOR = "Invalid or unknown cursor."
BAD_EXCHANGE = "Unknown exchange 'NOPE'."


def cursor_for(payload: object) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_list_exchanges(client: TestClient) -> None:
    response = client.get("/v1/exchanges", headers=DEMO)
    assert response.status_code == 200
    body = response.json()
    assert [ex["code"] for ex in body] == ["XHKG", "XLON", "XNAS", "XNYS", "XPAR", "XTKS"]
    assert body[5] == {
        "code": "XTKS",
        "name": "Tokyo Stock Exchange",
        "mic": "XTKS",
        "timezone": "Asia/Tokyo",
        "currency": "JPY",
        "sessions": [{"open": "09:00", "close": "11:30"}, {"open": "12:30", "close": "15:30"}],
    }


def test_get_exchange(client: TestClient) -> None:
    assert client.get("/v1/exchanges/xlon", headers=DEMO).json()["code"] == "XLON"
    missing = client.get("/v1/exchanges/nope", headers=DEMO)
    assert missing.status_code == 404
    error = error_of(missing.json())
    assert error["code"] == "exchange_not_found"
    assert error["message"] == "No exchange with code 'NOPE'."
    assert "param" not in error


def test_ticker_pagination_walks_every_ticker(client: TestClient) -> None:
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        query = f"?limit=25&cursor={cursor}" if cursor else "?limit=25"
        body = client.get(f"/v1/tickers{query}", headers=DEMO).json()
        assert body["total"] == 60
        seen += [t["symbol"] for t in body["data"]]
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break
        assert cursor == cursor_for({"after": seen[-1]})
    assert pages == 3
    assert len(seen) == 60
    assert seen == sorted(seen)


def test_ticker_defaults_and_shape(client: TestClient) -> None:
    body = client.get("/v1/tickers", headers=DEMO).json()
    assert len(body["data"]) == 20
    assert body["data"][0] == {
        "symbol": "0005.HK",
        "name": "HSBC Holdings plc",
        "exchange": "XHKG",
        "currency": "HKD",
        "sector": "Financials",
        "listed_on": "1991-04-01",
    }
    assert body["next_cursor"] == cursor_for({"after": body["data"][-1]["symbol"]})
    exact = client.get("/v1/tickers?limit=60", headers=DEMO).json()
    assert len(exact["data"]) == 60
    assert exact["next_cursor"] is None


def test_ticker_filters(client: TestClient) -> None:
    body = client.get("/v1/tickers?exchange=xtks&sector=AUTOMOTIVE", headers=DEMO).json()
    assert [t["symbol"] for t in body["data"]] == ["7203.T", "7267.T"]
    assert body["total"] == 2
    assert body["next_cursor"] is None
    paged = client.get("/v1/tickers?sector=Financials&limit=5", headers=DEMO).json()
    assert paged["total"] == 12
    second = client.get(
        f"/v1/tickers?sector=Financials&limit=5&cursor={paged['next_cursor']}", headers=DEMO
    ).json()
    assert [t["symbol"] for t in second["data"]] == ["BNP.PA", "GS", "HSBA.L", "JPM", "PYPL"]
    assert second["total"] == 12
    empty = client.get("/v1/tickers?exchange=&sector=&cursor=", headers=DEMO).json()
    assert empty["total"] == 60


def test_cursor_after_symbol_outside_the_filter(client: TestClient) -> None:
    cursor = cursor_for({"after": "aapl"})  # case-insensitive, need not match the filter
    body = client.get(f"/v1/tickers?exchange=XLON&cursor={cursor}", headers=DEMO).json()
    london = ["AZN.L", "BARC.L", "BP.L", "HSBA.L", "RR.L", "SHEL.L", "ULVR.L"]
    assert [t["symbol"] for t in body["data"]] == london
    padded = base64.urlsafe_b64encode(b'{"after": "XOM"}').decode()  # padding/spaces accepted
    assert padded.endswith("=")
    assert client.get(f"/v1/tickers?cursor={padded}", headers=DEMO).json() == {
        "data": [],
        "next_cursor": None,
        "total": 60,
    }


@pytest.mark.parametrize(
    ("query", "code", "param", "message"),
    [
        ("limit=0", "invalid_parameter", "limit", BAD_LIMIT),
        ("limit=101", "invalid_parameter", "limit", BAD_LIMIT),
        ("limit=abc", "invalid_parameter", "limit", BAD_LIMIT),
        ("exchange=NOPE", "invalid_parameter", "exchange", "Unknown exchange 'NOPE'."),
        ("sector=Toys", "invalid_parameter", "sector", "Unknown sector 'Toys'."),
        ("cursor=!!!", "invalid_cursor", "cursor", BAD_CURSOR),
        (f"cursor={cursor_for({'after': 'NOPE'})}", "invalid_cursor", "cursor", BAD_CURSOR),
        (f"cursor={cursor_for(['AAPL'])}", "invalid_cursor", "cursor", BAD_CURSOR),
        (f"cursor={cursor_for({'after': 5})}", "invalid_cursor", "cursor", BAD_CURSOR),
        ("cursor=e30", "invalid_cursor", "cursor", BAD_CURSOR),
        ("cursor=a", "invalid_cursor", "cursor", BAD_CURSOR),
        ("exchange=NOPE&sector=Toys&cursor=x", "invalid_parameter", "exchange", BAD_EXCHANGE),
    ],
)
def test_ticker_list_errors(
    client: TestClient, query: str, code: str, param: str, message: str
) -> None:
    response = client.get(f"/v1/tickers?{query}", headers=DEMO)
    assert response.status_code == 400
    assert error_of(response.json()) == {
        "code": code,
        "message": message,
        "request_id": response.json()["error"]["request_id"],
        "param": param,
    }


def test_get_ticker(client: TestClient) -> None:
    body = client.get("/v1/tickers/mc.pa", headers=DEMO).json()
    assert body == {
        "symbol": "MC.PA",
        "name": "LVMH Moët Hennessy Louis Vuitton SE",
        "exchange": "XPAR",
        "currency": "EUR",
        "sector": "Consumer Discretionary",
        "listed_on": "1987-06-03",
    }
    raw = client.get("/v1/tickers/MC.PA", headers=DEMO).content
    assert "Moët".encode() in raw  # raw UTF-8, not \u escapes
    missing = client.get("/v1/tickers/nope", headers=DEMO)
    assert missing.status_code == 404
    assert error_of(missing.json())["message"] == "No ticker with symbol 'NOPE'."
    assert error_of(missing.json())["code"] == "ticker_not_found"


def test_quote_while_open(client: TestClient, clock: ManualClock) -> None:
    clock.set_ns(at(2026, 10, 9, 11, 0, 0, tz="America/New_York", nanos=123_456_789))
    body = client.get("/v1/tickers/aapl/quote", headers=DEMO).json()
    assert body == {
        "symbol": "AAPL",
        "price": "227.48",
        "currency": "USD",
        "market_state": "open",
        "as_of": "2026-10-09T14:59:58.623456Z",
    }
    clock.advance(60)
    later = client.get("/v1/tickers/AAPL/quote", headers=DEMO).json()
    assert later["price"] == "227.48"
    assert later["as_of"] == "2026-10-09T15:00:58.623456Z"


def test_quote_while_closed(client: TestClient, clock: ManualClock) -> None:
    clock.set_ns(at(2026, 10, 10, 3, 0, tz="UTC"))  # Saturday
    for symbol, as_of, price in [
        ("AAPL", "2026-10-09T20:00:00.000000Z", "227.48"),
        ("7203.T", "2026-10-09T06:30:00.000000Z", "2741.50"),
        ("0700.HK", "2026-10-09T08:00:00.000000Z", "412.60"),
        ("SHEL.L", "2026-10-09T15:30:00.000000Z", "26.89"),
        ("MC.PA", "2026-10-09T15:30:00.000000Z", "652.30"),
    ]:
        body = client.get(f"/v1/tickers/{symbol}/quote", headers=DEMO).json()
        assert (body["market_state"], body["as_of"], body["price"]) == ("closed", as_of, price)
    clock.set_ns(at(2026, 11, 27, 14, 0, tz="America/New_York"))  # after the early close
    assert client.get("/v1/tickers/F/quote", headers=DEMO).json()["as_of"] == (
        "2026-11-27T18:00:00.000000Z"
    )
    assert client.get("/v1/tickers/nope/quote", headers=DEMO).status_code == 404


def test_server_time(client: TestClient, clock: ManualClock) -> None:
    clock.set_ns(at(2026, 10, 11, 1, 17, 26, nanos=844_021_678))
    assert client.get("/v1/time", headers=DEMO).json() == {
        "utc": "2026-10-11T01:17:26.8440216Z",
        "unix_ms": 1_791_681_446_844,
        "zone": "UTC",
    }
    clock.advance(1)
    assert client.get("/v1/time", headers=DEMO).json()["unix_ms"] == 1_791_681_447_844


def test_search_endpoint(client: TestClient) -> None:
    body = client.get("/v1/search?q=Toyota%20car", headers=DEMO).json()
    assert body["query"] == "Toyota car"
    assert body["results"][:2] == [
        {"symbol": "7203.T", "name": "Toyota Motor Corporation", "score": 0.65},
        {"symbol": "1211.HK", "name": "BYD Company Limited", "score": 0.25},
    ]
    assert [(r["symbol"], r["score"]) for r in body["results"][2:]] == [
        ("7267.T", 0.25),
        ("F", 0.25),
        ("GM", 0.25),
        ("RIVN", 0.25),
        ("TSLA", 0.25),
    ]
    assert len(client.get("/v1/search?q=inc", headers=DEMO).json()["results"]) == 10
    assert client.get("/v1/search?q=%21%21", headers=DEMO).json() == {"query": "!!", "results": []}
    for query, message in [
        ("", "Missing required parameter 'q'."),
        (
            "?q=" + "x" * 65,
            "Invalid value for parameter 'q': must be a string of 2 to 64 characters.",
        ),
    ]:
        error = error_of(client.get(f"/v1/search{query}", headers=DEMO).json())
        assert error["message"] == message
        assert error["param"] == "q"
