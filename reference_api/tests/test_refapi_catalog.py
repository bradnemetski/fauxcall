from __future__ import annotations

import re
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from fauxcall_reference_api.catalog import (
    EXCHANGES,
    SYNONYMS,
    Catalog,
    Exchange,
    Session,
    default_catalog,
    parse_ticker_table,
)

GROUND_TRUTH = Path(__file__).resolve().parents[1] / "GROUND_TRUTH.md"
CATALOG = default_catalog()


def test_sizes_and_order() -> None:
    assert [ex.code for ex in CATALOG.exchanges] == ["XHKG", "XLON", "XNAS", "XNYS", "XPAR", "XTKS"]
    assert len(CATALOG.tickers) == 60
    symbols = [t.symbol for t in CATALOG.tickers]
    assert symbols == sorted(symbols)
    assert len(set(symbols)) == 60


def test_referential_integrity() -> None:
    codes = {ex.code for ex in CATALOG.exchanges}
    for ticker in CATALOG.tickers:
        assert ticker.exchange in codes
        assert ticker.currency == CATALOG.exchange_of(ticker).currency
        assert ticker.sector in CATALOG.sectors
        assert ticker.price == ticker.price.quantize(Decimal("0.01"))
        assert ticker.price > 0
        assert ticker.symbol == ticker.symbol.upper()
    assert set(SYNONYMS.values()) <= set(CATALOG.sectors)
    assert {t.exchange for t in CATALOG.tickers} == codes


def test_sectors() -> None:
    assert CATALOG.sectors == (
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
    )
    assert CATALOG.sector("consumer staples") == "Consumer Staples"
    assert CATALOG.sector("nope") is None


def test_lookups_are_case_insensitive() -> None:
    assert CATALOG.exchange("xnys") is CATALOG.exchange("XNYS")
    assert CATALOG.exchange("NOPE") is None
    ticker = CATALOG.ticker("shel.l")
    assert ticker is not None
    assert ticker.symbol == "SHEL.L"
    assert CATALOG.ticker("NOPE") is None


def test_exchange_zone_and_us_calendar() -> None:
    nyse = CATALOG.exchange("XNYS")
    assert nyse is not None
    assert nyse.zone.key == "America/New_York"
    assert nyse.sessions == (Session(time(9, 30), time(16, 0)),)
    assert date(2026, 7, 3) in nyse.holidays
    assert nyse.early_closes == {date(2026, 11, 27): time(13, 0), date(2026, 12, 24): time(13, 0)}
    nasdaq = CATALOG.exchange("XNAS")
    assert nasdaq is not None
    assert nasdaq.holidays == nyse.holidays


def test_custom_catalog() -> None:
    exchange = Exchange(
        code="XTST",
        name="Test",
        mic="XTST",
        timezone="UTC",
        currency="USD",
        sessions=(Session(time(9), time(17)),),
        holidays=frozenset(),
    )
    catalog = Catalog([exchange], parse_ticker_table("zz | Zed | XTST | Misc | 2020-01-01 | 1.5"))
    (ticker,) = catalog.tickers
    assert ticker.symbol == "zz"
    assert ticker.price == Decimal("1.5")
    assert exchange.early_closes == {}


def _markdown_rows(heading: str) -> list[list[str]]:
    text = GROUND_TRUTH.read_text(encoding="utf-8")
    section = text.split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("|")
    ]
    return [row for row in rows[2:] if row]  # skip the header and the separator


def test_ground_truth_lists_every_ticker_exactly() -> None:
    rows = _markdown_rows("### Tickers")
    expected = [
        [
            t.symbol,
            t.name,
            t.exchange,
            t.currency,
            t.sector,
            t.listed_on.isoformat(),
            str(t.price),
        ]
        for t in CATALOG.tickers
    ]
    assert rows == expected


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda ex: ex.code)
def test_ground_truth_lists_every_calendar_exactly(exchange: Exchange) -> None:
    rows = {row[0]: row for row in _markdown_rows("### Exchanges")}
    sessions = ", ".join(f"{s.open:%H:%M}-{s.close:%H:%M}" for s in exchange.sessions)
    assert rows[exchange.code] == [
        exchange.code,
        exchange.name,
        exchange.mic,
        exchange.timezone,
        exchange.currency,
        sessions,
    ]
    holidays = {row[0]: row[1] for row in _markdown_rows("### Holidays (2026)")}
    documented = set(re.findall(r"\d{2}-\d{2}", holidays[exchange.code]))
    assert documented == {f"{day:%m-%d}" for day in exchange.holidays}
    assert all(day.year == 2026 for day in exchange.holidays)
    early = {
        (date.fromisoformat(day), time.fromisoformat(closes))
        for code, day, closes in _markdown_rows("### Early closes (2026)")
        if code == exchange.code
    }
    assert early == set(exchange.early_closes.items())
