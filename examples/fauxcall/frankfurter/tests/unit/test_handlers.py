"""Unit tests of the rate engine (handlers.py) and the real-header middleware."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx2
import pytest

from fauxcall_frankfurter import handlers
from fauxcall_frankfurter.app import build_app
from fauxcall_frankfurter.handlers import (
    History,
    Query,
    QueryError,
    check_pair,
    parse_date,
    parse_query,
    rebase,
    round_rate,
    ruby_split,
    ruby_to_f,
)
from fauxcall_frankfurter.middleware import response_headers, successor_link
from fauxcall_frankfurter.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

LINK_RATES = '<https://api.frankfurter.dev/v2/rates>; rel="successor-version"'


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_app(Settings(fast=True, log_level="WARNING"))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://fake") as http:
        yield http


async def at(client: httpx2.AsyncClient, session: str, moment: str) -> dict[str, str]:
    """Freeze ``session``'s clock at ``moment``; returns the session header to send."""
    response = await client.post(
        "/__fauxcall/clock", json={"op": "freeze", "at": moment, "session": session}
    )
    assert response.status_code == 200, response.text
    return {"X-Fauxcall-Session": session}


# -- numbers ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10", 10.0),
        ("10abc", 10.0),
        ("1,5", 1.0),
        ("1e2", 100.0),
        (" 5", 5.0),
        (".5", 0.5),
        ("1_000", 1000.0),
        ("1.", 1.0),
        ("-3", -3.0),
        ("abc", 0.0),
        ("", 0.0),
        ("NaN", 0.0),
        ("Infinity", 0.0),
        ("+", 0.0),
    ],
)
def test_ruby_to_f(text: str, expected: float) -> None:
    assert ruby_to_f(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (17881.4, 17881),
        (5000.5, 5001),
        (1341.494, 1341.49),
        (65.0086, 65.009),
        (16.53314, 16.5331),
        (0.831072, 0.83107),
        (0.0000123456, 0.000012),
        (Decimal("1083.965"), 1083.96),
        (Decimal("111.675"), 111.68),
        (Decimal("200369.4"), 200369),
        (Decimal("0.00000011206"), 0.0),
    ],
)
def test_round_rate(value: float | Decimal, expected: float) -> None:
    rounded = round_rate(value)
    assert rounded == expected
    assert isinstance(rounded, int) == isinstance(expected, int)


def test_round_rate_rejects_infinity() -> None:
    with pytest.raises(QueryError, match="Infinity"):
        round_rate(float("inf"))
    with pytest.raises(QueryError, match=r'invalid value for Float\(\): "NaN"'):
        round_rate(float("nan"))
    with pytest.raises(QueryError, match="Infinity"):
        round_rate(Decimal("1e400"))


def test_huge_values_round_their_float() -> None:
    assert round_rate(Decimal("1e24") * Decimal("20036.94")) == 20036939999999999200204947456


def test_divide_like_ruby() -> None:
    assert handlers.divide(1.0, 0.0) == float("inf")
    assert handlers.divide(0.0, 0.0) != handlers.divide(0.0, 0.0)  # NaN
    assert handlers.divide(3.0, 2.0) == 1.5


def test_ruby_split_drops_trailing_empties() -> None:
    assert ruby_split("USD,,GBP,,") == ["USD", "", "GBP"]
    assert ruby_split("") == []


# -- the query -------------------------------------------------------------------------------------


def test_parse_query_aliases_and_case() -> None:
    query = parse_query({"from": "usd", "base": "GBP", "to": "jpy", "symbols": "CHF"})
    assert query == Query(amount=1.0, base="USD", symbols=["JPY"])
    assert parse_query({}) == Query()


def test_parse_query_rejects_non_positive_amounts() -> None:
    for amount in ("0", "-1", "abc", ""):
        with pytest.raises(QueryError, match="invalid_amount"):
            parse_query({"amount": amount})


def test_bad_pair_needs_an_explicit_base() -> None:
    check_pair({"symbols": "EUR"}, Query(symbols=["EUR"]))
    with pytest.raises(QueryError, match="bad_currency_pair"):
        check_pair({"base": "EUR", "symbols": "EUR"}, Query(symbols=["EUR"]))


def test_parse_date() -> None:
    assert parse_date("2024-02-29") == date(2024, 2, 29)
    assert parse_date("0000-02-29") is None
    for text in ("2026-02-30", "2026-13-01", "0000-13-01"):
        with pytest.raises(QueryError, match="invalid_date"):
            parse_date(text)


def test_rebase_drops_days_without_the_base() -> None:
    query = Query(base="BGN", symbols=["USD"])
    assert rebase({"USD": 1.1}, query) is None
    assert rebase({"BGN": 1.9558}, query) is None  # nothing left but the base


def test_history_as_of() -> None:
    days = [date(2026, 1, 2), date(2026, 1, 5)]
    data = History(days=days, rates={day: {"USD": 1.1} for day in days})
    assert data.as_of(date(2026, 1, 5), []) is data
    assert data.as_of(None, []).days == []
    assert data.as_of(date(2026, 1, 3), []).days == [date(2026, 1, 2)]
    longer = data.as_of(date(2026, 1, 6), [date(2026, 1, 6)])
    assert longer.days == [*days, date(2026, 1, 6)]
    assert longer.rates[date(2026, 1, 6)] == {"USD": 1.1}
    assert longer.on_or_before(date(2026, 1, 1)) is None
    assert longer.on_or_before(date(2026, 1, 4)) == date(2026, 1, 2)


def test_rack_params() -> None:
    params = handlers.rack_params(
        [("symbols[]", "USD"), ("symbols[]", "GBP"), ("base[x]", "A"), ("base[y]", "B"), ("a", "1")]
    )
    assert params == {"symbols": ["USD", "GBP"], "base": {"x": "A", "y": "B"}, "a": "1"}


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"symbols": ["USD"]}, "undefined method 'upcase' for an instance of Array"),
        ({"amount": ["1"]}, "undefined method 'to_f' for an instance of Array"),
        (
            {"base": {"x": "USD"}},
            f"undefined method 'upcase' for an instance of {handlers.HASH_CLASS}",
        ),
    ],
)
def test_bracket_params_fail_like_ruby(params: dict[str, handlers.Param], message: str) -> None:
    with pytest.raises(QueryError) as caught:
        parse_query(params)
    assert caught.value.template == message


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("abc", "invalid_date"),
        ("", "invalid_date"),
        ("2026-01-01", "undefined method 'begin' for an instance of Date"),
        ("20260101", "undefined method 'begin' for an instance of Date"),
        (["x"], "no implicit conversion of Array into String"),
    ],
)
def test_date_param_on_periods(value: handlers.Param, message: str) -> None:
    with pytest.raises(QueryError) as caught:
        parse_query({"date": value}, period=True)
    assert caught.value.template == message
    assert parse_query({"date": value}) == Query()


# -- endpoints -------------------------------------------------------------------------------------


async def test_latest_rolls_over_at_the_ecb_publication(client: httpx2.AsyncClient) -> None:
    before = await at(client, "pub-before", "2026-10-12T14:00:00+00:00")  # 16:00 Berlin
    body = (await client.get("/v1/latest", headers=before)).json()
    assert body["date"] == "2026-10-09"
    after = await at(client, "pub-after", "2026-10-12T15:00:00+00:00")  # 17:00 Berlin
    body = (await client.get("/v1/latest", params={"symbols": "USD"}, headers=after)).json()
    assert body == {"amount": 1.0, "base": "EUR", "date": "2026-10-12", "rates": {"USD": 1.1206}}


async def test_weekends_and_holidays_keep_the_last_fixing(client: httpx2.AsyncClient) -> None:
    saturday = await at(client, "saturday", "2026-10-17T12:00:00+00:00")
    assert (await client.get("/v1/latest", headers=saturday)).json()["date"] == "2026-10-16"
    christmas = await at(client, "christmas", "2026-12-25T18:00:00+00:00")
    assert (await client.get("/v1/current", headers=christmas)).json()["date"] == "2026-12-24"


async def test_target_holidays_beyond_2028(client: httpx2.AsyncClient) -> None:
    christmas = await at(client, "christmas-2029", "2029-12-25T18:00:00+00:00")
    assert (await client.get("/v1/latest", headers=christmas)).json()["date"] == "2029-12-24"
    good_friday = await at(client, "good-friday-2035", "2035-03-23T18:00:00+00:00")
    assert (await client.get("/v1/latest", headers=good_friday)).json()["date"] == "2035-03-22"


async def test_synthesized_fixings_carry_the_last_recorded_rates(
    client: httpx2.AsyncClient,
) -> None:
    later = await at(client, "synth-week", "2026-10-20T18:00:00+00:00")
    body = (await client.get("/v1/2026-10-08..", params={"symbols": "USD"}, headers=later)).json()
    assert body["end_date"] == "2026-10-20"
    synthesized = [day for day in body["rates"] if day > "2026-10-09"]
    assert synthesized == [
        "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
        "2026-10-19", "2026-10-20",
    ]  # fmt: skip
    assert {body["rates"][day]["USD"] for day in synthesized} == {
        body["rates"]["2026-10-09"]["USD"]
    }


async def test_open_range_starts_after_today_is_not_found(client: httpx2.AsyncClient) -> None:
    sunday = await at(client, "range-sunday", "2026-10-11T08:00:00+00:00")
    response = await client.get("/v1/2026-10-12..", headers=sunday)
    assert response.status_code == 404
    tuesday = await at(client, "range-tuesday", "2026-10-13T15:00:00+00:00")
    body = (await client.get("/v1/2026-10-12..", params={"symbols": "USD"}, headers=tuesday)).json()
    assert list(body["rates"]) == ["2026-10-12", "2026-10-13"]


async def test_currencies_follow_the_clock(client: httpx2.AsyncClient) -> None:
    january = await at(client, "bgn-era", "2026-01-05T12:00:00+00:00")
    names = (await client.get("/v1/currencies", headers=january)).json()
    assert names["BGN"] == "Bulgarian Lev"
    before = await at(client, "before-ecb", "1998-06-01T12:00:00+00:00")
    response = await client.get("/v1/currencies", headers=before)
    assert response.status_code == 200
    assert response.json() == {}
    assert "etag" not in response.headers
    assert (await client.get("/v1/latest", headers=before)).status_code == 404


async def test_infinite_amount_is_a_float_domain_error(client: httpx2.AsyncClient) -> None:
    response = await client.get("/v1/latest", params={"amount": "1e400"})
    assert response.status_code == 422
    assert response.json() == {"message": "Infinity"}


async def test_errors_precedence(client: httpx2.AsyncClient) -> None:
    invalid = await client.get("/v1/2026-02-30", params={"base": "USD", "symbols": "USD"})
    assert invalid.json() == {"message": "invalid date"}
    year_zero = await client.get("/v1/0000-01-01..0000-01-05")
    assert year_zero.status_code == 404
    unknown = await client.get("/v1/abc..")
    assert unknown.json() == {"status": 404, "message": "not found"}


async def test_tiny_amount_with_rebase(client: httpx2.AsyncClient) -> None:
    nan = await client.get("/v1/latest", params={"amount": "0.0000001", "base": "USD"})
    assert nan.status_code == 422
    assert nan.json() == {"message": 'invalid value for Float(): "NaN"'}
    inf = await client.get(
        "/v1/latest", params={"amount": "0.0000001", "base": "USD", "symbols": "IDR,EUR"}
    )
    assert inf.json() == {"message": "Infinity"}


async def test_period_edge_inputs(client: httpx2.AsyncClient) -> None:
    year_zero = await client.get("/v1/0000-01-01..1999-01-08", params={"symbols": "USD"})
    assert year_zero.json()["start_date"] == "1999-01-04"
    dated = await client.get("/v1/2026-09-28..2026-10-02", params={"date": "2026-01-01"})
    assert dated.json() == {"message": "undefined method 'begin' for an instance of Date"}
    encoded = await client.get("/v1/2026%2D10%2D08")
    assert encoded.json() == {"status": 404, "message": "not found"}
    names = await client.get("/v1/%63urrencies")
    assert names.status_code == 404


async def test_clock_moved_back_hides_later_fixings(client: httpx2.AsyncClient) -> None:
    back = await at(client, "back-2020", "2020-06-01T12:00:00+00:00")
    latest = (await client.get("/v1/latest", headers=back)).json()
    assert latest["date"] == "2020-05-29"
    later = await client.get("/v1/2020-06-02", headers=back)
    assert later.json()["date"] == "2020-05-29"


async def test_etag_and_conditional_requests(client: httpx2.AsyncClient) -> None:
    first = await client.get("/v1/2026-10-08", params={"symbols": "USD"})
    tag = first.headers["etag"]
    assert tag == 'W/"' + handlers.etag(date(2026, 10, 8)).split('"')[1] + '"'
    again = await client.get("/v1/2026-10-08", headers={"If-None-Match": tag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == tag.removeprefix("W/")
    assert again.headers["cache-control"] == "public, max-age=86400"
    other = await client.get("/v1/2026-10-08", headers={"If-None-Match": '"nope"'})
    assert other.status_code == 200
    star = await client.get("/v1/2026-10-08", headers={"If-None-Match": "*"})
    assert star.status_code == 304


# -- middleware ------------------------------------------------------------------------------------


async def test_head_has_headers_but_no_body(client: httpx2.AsyncClient) -> None:
    response = await client.head("/v1/latest")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["link"] == LINK_RATES


async def test_cors(client: httpx2.AsyncClient) -> None:
    preflight = await client.options(
        "/v1/latest",
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-methods"] == "GET, OPTIONS"
    assert "content-type" not in preflight.headers
    simple = await client.get("/v1/latest", headers={"Origin": "https://example.com"})
    assert simple.headers["access-control-allow-origin"] == "*"
    assert simple.headers["vary"] == "Origin"


async def test_preflight_variants(client: httpx2.AsyncClient) -> None:
    echoed = await client.options(
        "/v1/latest",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Foo",
        },
    )
    assert echoed.headers["access-control-allow-headers"] == "X-Foo"
    refused = await client.options(
        "/v1/latest",
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
    )
    assert refused.status_code == 200
    assert "access-control-allow-origin" not in refused.headers
    plain = await client.options("/v1/latest", params={"symbols": "USD"})
    assert plain.json()["rates"] == {"USD": 1.1206}
    posted = await client.post("/v1/latest", params={"symbols": "USD"})
    assert posted.status_code == 200


async def test_preflight_outside_v1(client: httpx2.AsyncClient) -> None:
    preflight = await client.options(
        "/", headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"}
    )
    assert "deprecation" not in preflight.headers
    assert "x-robots-tag" not in preflight.headers


async def test_admin_api_is_untouched(client: httpx2.AsyncClient) -> None:
    response = await client.get("/__fauxcall/clock")
    assert "deprecation" not in response.headers


def test_header_rules() -> None:
    assert successor_link("/v2/rates") is None
    assert successor_link("/v1/openapi.json") == (
        '<https://api.frankfurter.dev/v2/openapi.json>; rel="successor-version"'
    )
    redirect = dict(response_headers("/v1/x", 304, [("cache-control", "private")], origin=False))
    assert redirect["cache-control"] == "private"
    bare = dict(response_headers("/", 304, [], origin=False))
    assert "cache-control" not in bare


def test_names_invariant(app: FastAPI) -> None:
    world = app.state.fauxcall.world.session("invariant")
    assert handlers.names_cover_quotes(world) == []
