"""End-to-end scenarios for the Frankfurter v1 fake.

Each runs against the fake (default), the real API (``--target=real``) or both with a compliance
report (``--target=parity``). Assertions check behavior that holds on both sides (relations
between calls, rounding rules, error shapes), never values that would change when the ECB
publishes; parity then compares every call's status, content type, shape and values.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient

LINK_RATES = '<https://api.frankfurter.dev/v2/rates>; rel="successor-version"'
APP_NOT_FOUND = {"message": "not found"}
ROUTE_NOT_FOUND = {"status": 404, "message": "not found"}


def utc_today() -> date:
    """``Date.today`` on the (UTC) server: the local date can be a day off."""
    return datetime.now(UTC).date()


def decimals(value: float) -> int:
    """Digits after the decimal point of a JSON number."""
    text = repr(value)
    return len(text.split(".")[1]) if "." in text and "e" not in text else 0


def assert_quoting_precision(rates: dict[str, float]) -> None:
    """Rebased/scaled rates follow Roundable: >5000 int, >80 2 dp, >20 3 dp, >1 4 dp, else 5 dp."""
    for code, value in rates.items():
        if value > 5000:
            assert isinstance(value, int), (code, value)
        elif value > 80:
            assert decimals(value) <= 2, (code, value)
        elif value > 20:
            assert decimals(value) <= 3, (code, value)
        elif value > 1:
            assert decimals(value) <= 4, (code, value)
        elif value > 0.0001:
            assert decimals(value) <= 5, (code, value)


# -- index documents ------------------------------------------------------------------------------


async def test_v1_index(api: ApiClient) -> None:
    for path in ("/v1", "/v1/"):
        response = await api.get(path)
        assert response.status_code == 200
        assert response.json()["status"] == "deprecated"
        assert response.headers["link"] == (
            '<https://api.frankfurter.dev/v2>; rel="successor-version"'
        )


async def test_site_index(api: ApiClient) -> None:
    response = await api.get("/")
    assert response.status_code == 200
    assert response.json()["versions"]["v1"]["status"] == "frozen"
    assert "deprecation" not in response.headers


# -- currencies -----------------------------------------------------------------------------------


async def test_currencies(api: ApiClient) -> None:
    response = await api.get("/v1/currencies")
    assert response.status_code == 200
    names = response.json()
    assert names["EUR"] == "Euro"
    assert names["USD"] == "United States Dollar"
    assert "BGN" not in names  # Bulgaria adopted the euro on 2026-01-01
    assert list(names) == sorted(names)
    assert response.headers["link"] == (
        '<https://api.frankfurter.dev/v2/currencies>; rel="successor-version"'
    )


async def test_currencies_match_latest_rates(api: ApiClient) -> None:
    names = (await api.get("/v1/currencies")).json()
    latest = (await api.get("/v1/latest")).json()
    assert set(names) == {*latest["rates"], "EUR"}


async def test_currencies_ignore_query(api: ApiClient) -> None:
    response = await api.get("/v1/currencies", params={"base": "USD", "symbols": "GBP"})
    assert response.status_code == 200
    assert "AUD" in response.json()


# -- latest ---------------------------------------------------------------------------------------


async def test_latest_default(api: ApiClient) -> None:
    response = await api.get("/v1/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["amount"] == 1.0
    assert body["base"] == "EUR"
    assert "EUR" not in body["rates"]
    assert list(body["rates"]) == sorted(body["rates"])
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.headers["cache-control"] == "public, max-age=86400"
    assert response.headers["deprecation"] == "@1779103800"
    assert response.headers["link"] == LINK_RATES
    assert response.headers["x-robots-tag"] == "noindex"
    digest = hashlib.md5(body["date"].encode(), usedforsecurity=False).hexdigest()
    assert response.headers["etag"] == f'W/"{digest}"'


async def test_latest_is_a_recent_working_day(api: ApiClient) -> None:
    body = (await api.get("/v1/latest", params={"symbols": "USD"})).json()
    day = date.fromisoformat(body["date"])
    assert day.weekday() < 5
    assert utc_today() - day <= timedelta(days=5)


async def test_current_is_latest(api: ApiClient) -> None:
    latest = (await api.get("/v1/latest", params={"symbols": "USD,JPY"})).json()
    current = (await api.get("/v1/current", params={"symbols": "USD,JPY"})).json()
    assert current == latest


async def test_latest_equals_its_date(api: ApiClient) -> None:
    latest = (await api.get("/v1/latest")).json()
    same = (await api.get(f"/v1/{latest['date']}")).json()
    assert same == latest


@pytest.mark.parametrize("base", ["USD", "JPY", "GBP", "CHF", "INR", "TRY", "IDR"])
async def test_latest_rebased(api: ApiClient, base: str) -> None:
    body = (await api.get("/v1/latest", params={"base": base})).json()
    assert body["base"] == base
    assert base not in body["rates"]
    assert "EUR" in body["rates"]
    assert_quoting_precision(body["rates"])


async def test_latest_symbols(api: ApiClient) -> None:
    body = (await api.get("/v1/latest", params={"symbols": "JPY,AUD,CHF"})).json()
    assert list(body["rates"]) == ["AUD", "CHF", "JPY"]


async def test_latest_rebased_symbols_include_eur(api: ApiClient) -> None:
    body = (await api.get("/v1/latest", params={"base": "USD", "symbols": "EUR,GBP"})).json()
    assert set(body["rates"]) == {"EUR", "GBP"}
    eur = (await api.get("/v1/latest", params={"symbols": "USD"})).json()["rates"]["USD"]
    assert body["rates"]["EUR"] == pytest.approx(1 / eur, rel=1e-4)


async def test_base_in_symbols_is_dropped(api: ApiClient) -> None:
    body = (await api.get("/v1/latest", params={"base": "USD", "symbols": "USD,EUR"})).json()
    assert list(body["rates"]) == ["EUR"]


@pytest.mark.parametrize("amount", ["10", "1.5", "0.001", "1000000", "250"])
async def test_amount(api: ApiClient, amount: str) -> None:
    unit = (await api.get("/v1/latest")).json()["rates"]
    body = (await api.get("/v1/latest", params={"amount": amount})).json()
    assert body["amount"] == float(amount)
    assert_quoting_precision(body["rates"])
    for code, rate in body["rates"].items():
        assert rate == pytest.approx(unit[code] * float(amount), rel=1e-3, abs=1e-5)


async def test_amount_with_rebase(api: ApiClient) -> None:
    body = (
        await api.get("/v1/latest", params={"amount": "1000000", "base": "USD", "symbols": "JPY"})
    ).json()
    assert isinstance(body["rates"]["JPY"], int)


async def test_amount_one_keeps_raw_rates(api: ApiClient) -> None:
    raw = (await api.get("/v1/latest")).json()
    explicit = (await api.get("/v1/latest", params={"amount": "1"})).json()
    assert explicit == raw


@pytest.mark.parametrize(
    ("amount", "parsed"), [("1e2", 100.0), ("10abc", 10.0), ("1,5", 1.0), (".5", 0.5)]
)
async def test_amount_parsed_like_ruby_to_f(api: ApiClient, amount: str, parsed: float) -> None:
    body = (await api.get("/v1/latest", params={"amount": amount, "symbols": "USD"})).json()
    assert body["amount"] == parsed


@pytest.mark.parametrize("amount", ["0", "-5", "abc", "", "NaN"])
async def test_invalid_amount(api: ApiClient, amount: str) -> None:
    response = await api.get("/v1/latest", params={"amount": amount})
    assert response.status_code == 422
    assert response.json() == {"message": "invalid amount"}
    assert response.headers["cache-control"] == "no-store"


async def test_lowercase_codes(api: ApiClient) -> None:
    lower = (await api.get("/v1/latest", params={"base": "usd", "symbols": "eur,gbp"})).json()
    upper = (await api.get("/v1/latest", params={"base": "USD", "symbols": "EUR,GBP"})).json()
    assert lower == upper


async def test_from_to_aliases_take_precedence(api: ApiClient) -> None:
    body = (
        await api.get(
            "/v1/latest", params={"from": "USD", "base": "GBP", "to": "JPY", "symbols": "CHF"}
        )
    ).json()
    assert body["base"] == "USD"
    assert list(body["rates"]) == ["JPY"]


async def test_last_repeated_parameter_wins(api: ApiClient) -> None:
    body = (await api.get("/v1/latest?symbols=USD&symbols=GBP&base=USD&base=JPY")).json()
    assert body["base"] == "JPY"
    assert list(body["rates"]) == ["GBP"]


async def test_unknown_symbols_are_ignored(api: ApiClient) -> None:
    body = (await api.get("/v1/latest", params={"symbols": "USD,XXX"})).json()
    assert list(body["rates"]) == ["USD"]
    spaced = (await api.get("/v1/latest", params={"symbols": "USD, GBP"})).json()
    assert list(spaced["rates"]) == ["USD"]


@pytest.mark.parametrize(
    "params",
    [
        {"base": "XXX"},
        {"base": "BGN"},
        {"base": ""},
        {"symbols": "XXX"},
        {"symbols": ""},
        {"symbols": "EUR"},
        {"base": "USD", "symbols": "USD,USD"},
    ],
)
async def test_latest_not_found(api: ApiClient, params: dict[str, str]) -> None:
    response = await api.get("/v1/latest", params=params)
    assert response.status_code == 404
    assert response.json() == APP_NOT_FOUND
    assert "etag" not in response.headers


@pytest.mark.parametrize(
    "params",
    [
        {"base": "USD", "symbols": "USD"},
        {"base": "EUR", "symbols": "EUR"},
        {"base": "usd", "symbols": "USD"},
        {"from": "JPY", "to": "JPY"},
        {"base": "XXX", "symbols": "XXX"},
    ],
)
async def test_bad_currency_pair(api: ApiClient, params: dict[str, str]) -> None:
    response = await api.get("/v1/latest", params=params)
    assert response.status_code == 422
    assert response.json() == {"message": "bad currency pair"}


async def test_amount_error_wins(api: ApiClient) -> None:
    response = await api.get("/v1/latest", params={"amount": "0", "base": "XXX"})
    assert response.json() == {"message": "invalid amount"}


async def test_head_latest(api: ApiClient) -> None:
    response = await api.request("HEAD", "/v1/latest")
    assert response.status_code == 200
    assert response.content == b""


async def test_cors_headers(api: ApiClient) -> None:
    response = await api.get(
        "/v1/latest", params={"symbols": "USD"}, headers={"Origin": "https://example.com"}
    )
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-allow-methods"] == "GET, OPTIONS"


async def test_cors_preflight(api: ApiClient) -> None:
    response = await api.request(
        "OPTIONS",
        "/v1/latest",
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-max-age"] == "7200"


# -- a date ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("asked", "answered"),
    [
        ("2026-10-08", "2026-10-08"),
        ("2024-02-29", "2024-02-29"),
        ("2025-12-25", "2025-12-24"),
        ("2025-12-26", "2025-12-24"),
        ("2026-01-01", "2025-12-31"),
        ("2026-04-03", "2026-04-02"),
        ("2026-04-06", "2026-04-02"),
        ("2026-05-01", "2026-04-30"),
        ("1999-01-04", "1999-01-04"),
        ("2020-03-15", "2020-03-13"),
    ],
)
async def test_date_falls_back_to_previous_fixing(
    api: ApiClient, asked: str, answered: str
) -> None:
    response = await api.get(f"/v1/{asked}")
    assert response.status_code == 200
    assert response.json()["date"] == answered


@pytest.mark.parametrize(
    ("day", "present", "absent"),
    [
        ("1999-01-04", ["GRD", "CYP", "EEK", "MTL"], ["BGN", "ILS"]),
        ("2005-01-03", ["CYP", "ARS", "BGN", "HRK"], ["GRD"]),
        ("2015-03-02", ["RUB", "HRK"], ["LTL", "CYP"]),
        ("2022-03-02", ["BGN", "HRK"], ["RUB"]),
        ("2025-12-31", ["BGN"], ["HRK"]),
        ("2026-01-02", ["USD"], ["BGN"]),
    ],
)
async def test_currency_set_depends_on_the_date(
    api: ApiClient, day: str, present: list[str], absent: list[str]
) -> None:
    rates = (await api.get(f"/v1/{day}")).json()["rates"]
    assert all(code in rates for code in present)
    assert not any(code in rates for code in absent)


async def test_historic_rebase(api: ApiClient) -> None:
    body = (
        await api.get("/v1/2010-06-15", params={"base": "USD", "symbols": "GBP,JPY,EUR"})
    ).json()
    assert body == {
        "amount": 1.0,
        "base": "USD",
        "date": "2010-06-15",
        "rates": {"EUR": 0.81579, "GBP": 0.67874, "JPY": 91.18},
    }


async def test_double_rounding_of_cross_rates(api: ApiClient) -> None:
    body = (await api.get("/v1/2015-03-02", params={"amount": "250", "base": "GBP"})).json()
    assert body["rates"]["NZD"] == 510.94
    assert body["rates"]["DZD"] == 36520
    old = (await api.get("/v1/1999-01-04", params={"base": "USD", "amount": "3.5"})).json()
    assert old["rates"]["EUR"] == 2.9688


async def test_legacy_base(api: ApiClient) -> None:
    body = (await api.get("/v1/2005-01-03", params={"base": "CYP", "symbols": "EUR,USD"})).json()
    assert body["base"] == "CYP"
    assert set(body["rates"]) == {"EUR", "USD"}


async def test_carry_forward_follows_the_base(api: ApiClient) -> None:
    body = (await api.get("/v1/2026-01-05", params={"base": "BGN"})).json()
    assert body == {"amount": 1.0, "base": "BGN", "date": "2025-12-31", "rates": {"EUR": 0.5113}}


async def test_carry_forward_first_group_wins(api: ApiClient) -> None:
    bgn = (await api.get("/v1/2026-01-05", params={"symbols": "BGN,USD"})).json()
    assert bgn["date"] == "2025-12-31"
    assert list(bgn["rates"]) == ["BGN"]
    full = (await api.get("/v1/2008-01-03")).json()
    assert full["date"] == "2008-01-03"
    assert "CYP" not in full["rates"]
    cyp = (await api.get("/v1/2008-01-03", params={"base": "CYP"})).json()
    assert cyp["date"] == "2007-12-31"
    assert set(cyp["rates"]) == {"EUR", "MTL"}


@pytest.mark.parametrize(
    ("day", "status"),
    [("2026-01-14", 200), ("2026-01-15", 404)],
)
async def test_lookback_is_fourteen_days(api: ApiClient, day: str, status: int) -> None:
    response = await api.get(f"/v1/{day}", params={"base": "BGN"})
    assert response.status_code == status


async def test_near_future_date_carries_the_last_fixing(api: ApiClient) -> None:
    latest = (await api.get("/v1/latest", params={"symbols": "USD"})).json()
    tomorrow = utc_today() + timedelta(days=1)
    body = (await api.get(f"/v1/{tomorrow.isoformat()}", params={"symbols": "USD"})).json()
    assert body == latest


@pytest.mark.parametrize("day", ["1999-01-01", "1990-01-01", "2100-01-01"])
async def test_date_without_data(api: ApiClient, day: str) -> None:
    response = await api.get(f"/v1/{day}")
    assert response.status_code == 404
    assert response.json() == APP_NOT_FOUND


@pytest.mark.parametrize("day", ["2026-13-01", "2026-02-30", "2025-02-29"])
async def test_invalid_date(api: ApiClient, day: str) -> None:
    response = await api.get(f"/v1/{day}")
    assert response.status_code == 422
    assert response.json() == {"message": "invalid date"}


async def test_invalid_date_beats_bad_pair(api: ApiClient) -> None:
    response = await api.get("/v1/2026-02-30", params={"base": "USD", "symbols": "USD"})
    assert response.json() == {"message": "invalid date"}


@pytest.mark.parametrize(
    "segment", ["abc", "20261009", "2026-10-9", "2026-1-05", "2026-10-09T00:00:00", "2026"]
)
async def test_malformed_date_is_an_unknown_route(api: ApiClient, segment: str) -> None:
    response = await api.get(f"/v1/{segment}")
    assert response.status_code == 404
    assert response.json() == ROUTE_NOT_FOUND
    assert response.headers["content-type"] == "application/json"
    assert response.headers["link"] == LINK_RATES


# -- periods --------------------------------------------------------------------------------------


async def test_period(api: ApiClient) -> None:
    response = await api.get("/v1/2026-09-28..2026-10-09")
    body = response.json()
    assert body["start_date"] == "2026-09-28"
    assert body["end_date"] == "2026-10-09"
    assert list(body["rates"]) == sorted(body["rates"])
    assert len(body["rates"]) == 10
    digest = hashlib.md5(b"2026-10-09", usedforsecurity=False).hexdigest()
    assert response.headers["etag"] == f'W/"{digest}"'


async def test_period_days_match_single_dates(api: ApiClient) -> None:
    period = (await api.get("/v1/2026-10-05..2026-10-09", params={"symbols": "USD,GBP"})).json()
    for day, rates in list(period["rates"].items())[:2]:
        single = (await api.get(f"/v1/{day}", params={"symbols": "USD,GBP"})).json()
        assert single["rates"] == rates


@pytest.mark.parametrize(
    ("path", "start", "end"),
    [
        ("/v1/2026-10-03..2026-10-09", "2026-10-02", "2026-10-09"),
        ("/v1/2026-10-05..2026-10-11", "2026-10-05", "2026-10-09"),
        ("/v1/2026-10-10..2026-10-11", "2026-10-09", "2026-10-09"),
        ("/v1/2026-10-08..2026-10-08", "2026-10-08", "2026-10-08"),
        ("/v1/2026-04-06..2026-04-10", "2026-04-02", "2026-04-10"),
        ("/v1/1998-12-01..1999-01-08", "1999-01-04", "1999-01-08"),
        ("/v1/2024-01-01..2024-12-31", "2023-12-29", "2024-12-31"),
    ],
)
async def test_period_boundaries(api: ApiClient, path: str, start: str, end: str) -> None:
    body = (await api.get(path, params={"symbols": "USD"})).json()
    assert (body["start_date"], body["end_date"]) == (start, end)


async def test_long_period_is_daily(api: ApiClient) -> None:
    body = (await api.get("/v1/2023-01-01..2024-12-31", params={"symbols": "USD"})).json()
    assert len(body["rates"]) == 512


async def test_period_rebased_with_amount(api: ApiClient) -> None:
    body = (
        await api.get(
            "/v1/2026-09-28..2026-10-02",
            params={"base": "USD", "symbols": "EUR,JPY,IDR", "amount": "100"},
        )
    ).json()
    assert body["amount"] == 100.0
    for rates in body["rates"].values():
        assert isinstance(rates["IDR"], int)
        assert_quoting_precision(rates)


async def test_period_drops_days_without_symbols_or_base(api: ApiClient) -> None:
    symbols = (await api.get("/v1/2025-12-29..2026-01-09", params={"symbols": "BGN"})).json()
    assert symbols["end_date"] == "2025-12-31"
    base = (
        await api.get("/v1/2025-12-29..2026-01-06", params={"base": "BGN", "symbols": "USD,EUR"})
    ).json()
    assert list(base["rates"]) == ["2025-12-29", "2025-12-30", "2025-12-31"]
    mixed = (await api.get("/v1/2025-12-29..2026-01-05", params={"symbols": "BGN,USD"})).json()
    assert mixed["rates"]["2026-01-02"] == {"USD": 1.1721}


@pytest.mark.parametrize(
    "path",
    [
        "/v1/2026-10-09..2026-10-01",
        "/v1/1998-01-01..1998-12-31",
        "/v1/2027-01-01..2027-02-01",
        "/v1/2026-01-02..2026-01-09?symbols=BGN",
        "/v1/2026-09-28..2026-10-02?base=XYZ",
    ],
)
async def test_period_not_found(api: ApiClient, path: str) -> None:
    response = await api.get(path)
    assert response.status_code == 404
    assert response.json() == APP_NOT_FOUND


async def test_period_starting_tomorrow_is_not_found(api: ApiClient) -> None:
    tomorrow = utc_today() + timedelta(days=1)
    closed = await api.get(f"/v1/{tomorrow.isoformat()}..{(tomorrow + timedelta(days=7))}")
    assert closed.status_code == 404
    opened = await api.get(f"/v1/{tomorrow.isoformat()}..")
    assert opened.status_code == 404


@pytest.mark.parametrize(
    ("path", "status", "body"),
    [
        ("/v1/2026-13-01..2026-10-01", 422, {"message": "invalid date"}),
        ("/v1/2026-10-01..2026-02-30", 422, {"message": "invalid date"}),
        ("/v1/2026-02-30..", 422, {"message": "invalid date"}),
        ("/v1/2026-09-28..2026-10-02?amount=-1", 422, {"message": "invalid amount"}),
        ("/v1/abc..2026-10-01", 404, ROUTE_NOT_FOUND),
        ("/v1/2026-10-01..abc", 404, ROUTE_NOT_FOUND),
        ("/v1/abc..", 404, ROUTE_NOT_FOUND),
        ("/v1/2026-10-01...2026-10-09", 404, ROUTE_NOT_FOUND),
        ("/v1/2026-10-01..2026-10-09/", 404, ROUTE_NOT_FOUND),
    ],
)
async def test_period_errors(
    api: ApiClient, path: str, status: int, body: dict[str, object]
) -> None:
    response = await api.get(path)
    assert response.status_code == status
    assert response.json() == body


async def test_open_period(api: ApiClient) -> None:
    latest = (await api.get("/v1/latest", params={"symbols": "USD"})).json()
    body = (await api.get("/v1/2026-09-28..", params={"symbols": "USD"})).json()
    assert body["start_date"] == "2026-09-28"
    assert body["end_date"] == latest["date"]
    assert body["rates"][latest["date"]] == latest["rates"]


async def test_open_period_from_the_weekend(api: ApiClient) -> None:
    latest = (await api.get("/v1/latest", params={"symbols": "USD"})).json()
    today = utc_today().isoformat()
    body = (await api.get(f"/v1/{today}..", params={"symbols": "USD"})).json()
    assert body["end_date"] == latest["date"]


# -- routing --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/v1/latest/", "/v1/LATEST", "/v1/Currencies", "/v1/currencies/", "/v1/2026-10-08/"]
)
async def test_routing_is_strict(api: ApiClient, path: str) -> None:
    response = await api.get(path)
    assert response.status_code == 404
    assert response.json() == ROUTE_NOT_FOUND
    assert response.headers["deprecation"] == "@1779103800"


async def test_unknown_route_outside_v1(api: ApiClient) -> None:
    response = await api.get("/currencies")
    assert response.status_code == 404
    assert response.json() == ROUTE_NOT_FOUND
    assert "deprecation" not in response.headers
    assert response.headers["cache-control"] == "no-store"


# -- edge inputs (review probes 000199-000219) ----------------------------------------------------


async def test_tiny_amount_with_rebase_fails_like_ruby(api: ApiClient) -> None:
    nan = await api.get("/v1/latest", params={"amount": "0.0000001", "base": "USD"})
    assert nan.status_code == 422
    assert nan.json() == {"message": 'invalid value for Float(): "NaN"'}
    inf = await api.get(
        "/v1/latest", params={"amount": "0.0000001", "base": "USD", "symbols": "IDR,EUR"}
    )
    assert inf.json() == {"message": "Infinity"}
    overflow = await api.get("/v1/latest", params={"amount": "1e400"})
    assert overflow.json() == {"message": "Infinity"}


async def test_huge_amounts(api: ApiClient) -> None:
    # recording 000201: 2026-10-09, IDR 20036.94
    body = (await api.get("/v1/2026-10-09", params={"amount": "1e24", "symbols": "IDR"})).json()
    assert body["rates"]["IDR"] == 20036939999999999200204947456
    assert body["amount"] == 1e24


async def test_year_zero_range_start(api: ApiClient) -> None:
    body = (await api.get("/v1/0000-01-01..1999-01-08", params={"symbols": "USD"})).json()
    assert body["start_date"] == "1999-01-04"


@pytest.mark.parametrize(
    ("value", "message"),
    [("abc", "invalid date"), ("2026-01-01", "undefined method 'begin' for an instance of Date")],
)
async def test_date_parameter_on_periods(api: ApiClient, value: str, message: str) -> None:
    response = await api.get("/v1/2026-09-28..2026-10-02", params={"date": value})
    assert response.status_code == 422
    assert response.json() == {"message": message}
    ignored = await api.get("/v1/latest", params={"date": value, "symbols": "USD"})
    assert ignored.status_code == 200


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("symbols%5B%5D=USD", "undefined method 'upcase' for an instance of Array"),
        ("amount%5B%5D=1", "undefined method 'to_f' for an instance of Array"),
        (
            "base%5Bx%5D=USD",
            (
                "undefined method 'upcase' for an instance of "
                "Roda::RodaPlugins::IndifferentParams::QueryParser::Params"
            ),
        ),
    ],
)
async def test_bracketed_parameters(api: ApiClient, query: str, message: str) -> None:
    response = await api.get(f"/v1/latest?{query}")
    assert response.status_code == 422
    assert response.json() == {"message": message}


async def test_first_carry_forward_group_quirks(api: ApiClient) -> None:
    body = (await api.get("/v1/2020-11-05")).json()
    assert body["date"] == "2020-10-30"
    assert set(body["rates"]) == {"ARS", "DZD", "MAD", "TWD"}
    rub = (await api.get("/v1/2022-03-07", params={"base": "RUB"})).json()
    assert rub == {"amount": 1.0, "base": "RUB", "date": "2022-03-01", "rates": {"EUR": 0.00853}}


async def test_percent_encoded_path(api: ApiClient) -> None:
    response = await api.get("/v1/2026%2D10%2D08")
    assert response.status_code == 404
    assert response.json() == ROUTE_NOT_FOUND


async def test_conditional_get(api: ApiClient) -> None:
    tag = hashlib.md5(b"2026-10-08", usedforsecurity=False).hexdigest()
    for sent in (f'W/"{tag}"', f'"{tag}"'):
        response = await api.get("/v1/2026-10-08", headers={"If-None-Match": sent})
        assert response.status_code == 304
        assert response.headers["cache-control"] == "public, max-age=86400"
        assert response.headers["etag"] == f'"{tag}"'


async def test_preflight_echoes_requested_headers(api: ApiClient) -> None:
    response = await api.request(
        "OPTIONS",
        "/v1/latest",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Foo",
        },
    )
    assert response.headers["access-control-allow-headers"] == "X-Foo"


async def test_preflight_for_a_disallowed_method(api: ApiClient) -> None:
    response = await api.request(
        "OPTIONS",
        "/v1/latest",
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


async def test_plain_options_is_served_like_get(api: ApiClient) -> None:
    response = await api.request("OPTIONS", "/v1/latest", params={"symbols": "USD"})
    assert response.status_code == 200
    assert set(response.json()["rates"]) == {"USD"}


async def test_openapi_spec(api: ApiClient) -> None:
    response = await api.request("HEAD", "/v1/openapi.json")
    assert response.status_code == 200
    assert response.headers["link"] == (
        '<https://api.frankfurter.dev/v2/openapi.json>; rel="successor-version"'
    )
    assert "x-robots-tag" not in response.headers
