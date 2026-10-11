"""Static seed data: exchanges (with 2026 trading calendars), tickers, sectors and search synonyms.

This data never changes at runtime. ``GROUND_TRUTH.md`` reproduces it; a test keeps the two in sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from functools import cached_property
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class Session:
    """One trading session in exchange-local wall-clock time: ``[open, close)``."""

    open: time
    close: time


@dataclass(frozen=True)
class Exchange:
    """A stock exchange and its trading calendar."""

    code: str
    name: str
    mic: str
    timezone: str
    currency: str
    sessions: tuple[Session, ...]
    holidays: frozenset[date]
    early_closes: Mapping[date, time] = field(default_factory=dict)

    @cached_property
    def zone(self) -> ZoneInfo:
        """The exchange's IANA time zone."""
        return ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class Ticker:
    """A listed instrument. ``price`` is its static reference price (never changes)."""

    symbol: str
    name: str
    exchange: str
    currency: str
    sector: str
    listed_on: date
    price: Decimal


def _days(*isodates: str) -> frozenset[date]:
    return frozenset(date.fromisoformat(text) for text in isodates)


def _sessions(*spans: tuple[str, str]) -> tuple[Session, ...]:
    return tuple(
        Session(time.fromisoformat(opens), time.fromisoformat(closes)) for opens, closes in spans
    )


_US_HOLIDAYS_2026: Final = _days(
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # Martin Luther King Jr. Day
    "2026-02-16",  # Washington's Birthday
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving Day
    "2026-12-25",  # Christmas Day
)
_US_EARLY_CLOSES_2026: Final = {
    date(2026, 11, 27): time(13, 0),  # day after Thanksgiving
    date(2026, 12, 24): time(13, 0),  # Christmas Eve
}

EXCHANGES: Final[tuple[Exchange, ...]] = (
    Exchange(
        code="XHKG",
        name="Hong Kong Stock Exchange",
        mic="XHKG",
        timezone="Asia/Hong_Kong",
        currency="HKD",
        sessions=_sessions(("09:30", "12:00"), ("13:00", "16:00")),
        holidays=_days(
            "2026-01-01",
            "2026-02-17",
            "2026-02-18",
            "2026-02-19",
            "2026-04-03",
            "2026-04-06",
            "2026-04-07",
            "2026-05-01",
            "2026-05-25",
            "2026-06-19",
            "2026-07-01",
            "2026-10-01",
            "2026-10-19",
            "2026-12-25",
            "2026-12-28",
        ),
    ),
    Exchange(
        code="XLON",
        name="London Stock Exchange",
        mic="XLON",
        timezone="Europe/London",
        currency="GBP",
        sessions=_sessions(("08:00", "16:30")),
        holidays=_days(
            "2026-01-01",
            "2026-04-03",
            "2026-04-06",
            "2026-05-04",
            "2026-05-25",
            "2026-08-31",
            "2026-12-25",
            "2026-12-28",
        ),
    ),
    Exchange(
        code="XNAS",
        name="Nasdaq Stock Market",
        mic="XNAS",
        timezone="America/New_York",
        currency="USD",
        sessions=_sessions(("09:30", "16:00")),
        holidays=_US_HOLIDAYS_2026,
        early_closes=_US_EARLY_CLOSES_2026,
    ),
    Exchange(
        code="XNYS",
        name="New York Stock Exchange",
        mic="XNYS",
        timezone="America/New_York",
        currency="USD",
        sessions=_sessions(("09:30", "16:00")),
        holidays=_US_HOLIDAYS_2026,
        early_closes=_US_EARLY_CLOSES_2026,
    ),
    Exchange(
        code="XPAR",
        name="Euronext Paris",
        mic="XPAR",
        timezone="Europe/Paris",
        currency="EUR",
        sessions=_sessions(("09:00", "17:30")),
        holidays=_days("2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01", "2026-12-25"),
    ),
    Exchange(
        code="XTKS",
        name="Tokyo Stock Exchange",
        mic="XTKS",
        timezone="Asia/Tokyo",
        currency="JPY",
        sessions=_sessions(("09:00", "11:30"), ("12:30", "15:30")),
        holidays=_days(
            "2026-01-01",
            "2026-01-02",
            "2026-01-12",
            "2026-02-11",
            "2026-02-23",
            "2026-03-20",
            "2026-04-29",
            "2026-05-04",
            "2026-05-05",
            "2026-05-06",
            "2026-07-20",
            "2026-08-11",
            "2026-09-21",
            "2026-09-22",
            "2026-09-23",
            "2026-10-12",
            "2026-11-03",
            "2026-11-23",
            "2026-12-31",
        ),
    ),
)

SYNONYMS: Final[Mapping[str, str]] = {
    "car": "Automotive",
    "cars": "Automotive",
    "auto": "Automotive",
    "autos": "Automotive",
    "automaker": "Automotive",
    "vehicle": "Automotive",
    "vehicles": "Automotive",
    "ev": "Automotive",
    "chip": "Semiconductors",
    "chips": "Semiconductors",
    "chipmaker": "Semiconductors",
    "semis": "Semiconductors",
    "semiconductor": "Semiconductors",
    "bank": "Financials",
    "banks": "Financials",
    "banking": "Financials",
    "finance": "Financials",
    "fintech": "Financials",
    "payments": "Financials",
    "oil": "Energy",
    "gas": "Energy",
    "petroleum": "Energy",
    "pharma": "Healthcare",
    "drug": "Healthcare",
    "drugs": "Healthcare",
    "biotech": "Healthcare",
    "medicine": "Healthcare",
    "tech": "Technology",
    "software": "Technology",
    "cloud": "Technology",
    "food": "Consumer Staples",
    "beverage": "Consumer Staples",
    "beverages": "Consumer Staples",
    "grocery": "Consumer Staples",
    "retail": "Consumer Discretionary",
    "ecommerce": "Consumer Discretionary",
    "luxury": "Consumer Discretionary",
    "media": "Communication Services",
    "social": "Communication Services",
    "streaming": "Communication Services",
    "telecom": "Communication Services",
    "aerospace": "Industrials",
    "aircraft": "Industrials",
    "machinery": "Industrials",
}

# symbol | name | exchange | sector | listed_on | price
_TICKER_TABLE: Final = """
0005.HK | HSBC Holdings plc | XHKG | Financials | 1991-04-01 | 68.35
0388.HK | Hong Kong Exchanges and Clearing Limited | XHKG | Financials | 2000-06-27 | 298.40
0700.HK | Tencent Holdings Limited | XHKG | Communication Services | 2004-06-16 | 412.60
0981.HK | Semiconductor Manufacturing International | XHKG | Semiconductors | 2004-03-18 | 23.85
1211.HK | BYD Company Limited | XHKG | Automotive | 2002-07-31 | 287.20
6758.T | Sony Group Corporation | XTKS | Technology | 1958-12-01 | 2862.50
6861.T | Keyence Corporation | XTKS | Industrials | 1987-10-21 | 65890.00
7203.T | Toyota Motor Corporation | XTKS | Automotive | 1949-05-16 | 2741.50
7267.T | Honda Motor Co., Ltd. | XTKS | Automotive | 1957-12-01 | 1563.00
8035.T | Tokyo Electron Limited | XTKS | Semiconductors | 1980-10-01 | 24315.00
8306.T | Mitsubishi UFJ Financial Group, Inc. | XTKS | Financials | 2001-04-02 | 1687.50
9984.T | SoftBank Group Corp. | XTKS | Communication Services | 1994-07-22 | 8954.00
9988.HK | Alibaba Group Holding Limited | XHKG | Consumer Discretionary | 2019-11-26 | 83.15
AAPL | Apple Inc. | XNAS | Technology | 1980-12-12 | 227.48
ADBE | Adobe Inc. | XNAS | Technology | 1986-08-20 | 512.37
AIR.PA | Airbus SE | XPAR | Industrials | 2000-07-10 | 142.36
AMD | Advanced Micro Devices, Inc. | XNAS | Semiconductors | 1972-09-27 | 158.92
AMGN | Amgen Inc. | XNAS | Healthcare | 1983-06-17 | 301.15
AMZN | Amazon.com, Inc. | XNAS | Consumer Discretionary | 1997-05-15 | 186.51
AVGO | Broadcom Inc. | XNAS | Semiconductors | 2009-08-06 | 171.24
AZN.L | AstraZeneca PLC | XLON | Healthcare | 1999-04-06 | 118.46
BA | The Boeing Company | XNYS | Industrials | 1962-01-02 | 154.20
BAC | Bank of America Corporation | XNYS | Financials | 1973-09-04 | 39.68
BARC.L | Barclays PLC | XLON | Financials | 1985-05-01 | 2.41
BNP.PA | BNP Paribas SA | XPAR | Financials | 1993-10-18 | 61.74
BP.L | BP p.l.c. | XLON | Energy | 1954-03-01 | 4.12
CAT | Caterpillar Inc. | XNYS | Industrials | 1929-12-02 | 352.17
COST | Costco Wholesale Corporation | XNAS | Consumer Staples | 1985-12-05 | 893.40
CSCO | Cisco Systems, Inc. | XNAS | Technology | 1990-02-16 | 54.83
CVX | Chevron Corporation | XNYS | Energy | 1921-06-24 | 147.55
DIS | The Walt Disney Company | XNYS | Communication Services | 1957-11-12 | 92.46
F | Ford Motor Company | XNYS | Automotive | 1956-03-07 | 10.58
GM | General Motors Company | XNYS | Automotive | 2010-11-18 | 47.91
GOOGL | Alphabet Inc. Class A | XNAS | Communication Services | 2004-08-19 | 163.24
GS | The Goldman Sachs Group, Inc. | XNYS | Financials | 1999-05-04 | 497.30
HSBA.L | HSBC Holdings plc | XLON | Financials | 1992-07-10 | 7.08
INTC | Intel Corporation | XNAS | Semiconductors | 1971-10-13 | 22.71
JNJ | Johnson & Johnson | XNYS | Healthcare | 1944-09-25 | 158.74
JPM | JPMorgan Chase & Co. | XNYS | Financials | 1969-03-05 | 211.42
KO | The Coca-Cola Company | XNYS | Consumer Staples | 1919-09-05 | 69.83
MC.PA | LVMH Moët Hennessy Louis Vuitton SE | XPAR | Consumer Discretionary | 1987-06-03 | 652.30
META | Meta Platforms, Inc. | XNAS | Communication Services | 2012-05-18 | 572.81
MSFT | Microsoft Corporation | XNAS | Technology | 1986-03-13 | 415.26
NFLX | Netflix, Inc. | XNAS | Communication Services | 2002-05-23 | 701.05
NVDA | NVIDIA Corporation | XNAS | Semiconductors | 1999-01-22 | 131.60
PEP | PepsiCo, Inc. | XNAS | Consumer Staples | 1972-06-01 | 168.32
PFE | Pfizer Inc. | XNYS | Healthcare | 1944-01-03 | 28.97
PYPL | PayPal Holdings, Inc. | XNAS | Financials | 2015-07-20 | 78.64
QCOM | QUALCOMM Incorporated | XNAS | Semiconductors | 1991-12-13 | 165.09
RIVN | Rivian Automotive, Inc. | XNAS | Automotive | 2021-11-10 | 11.87
RR.L | Rolls-Royce Holdings plc | XLON | Industrials | 2011-05-23 | 5.37
SAN.PA | Sanofi | XPAR | Healthcare | 1999-08-20 | 91.48
SHEL.L | Shell plc | XLON | Energy | 2005-07-20 | 26.89
TSLA | Tesla, Inc. | XNAS | Automotive | 2010-06-29 | 249.83
TSM | Taiwan Semiconductor Manufacturing Co., Ltd. | XNYS | Semiconductors | 1997-10-08 | 186.12
TTE.PA | TotalEnergies SE | XPAR | Energy | 1973-10-25 | 58.92
ULVR.L | Unilever PLC | XLON | Consumer Staples | 1930-01-02 | 47.15
V | Visa Inc. | XNYS | Financials | 2008-03-19 | 281.66
WFC | Wells Fargo & Company | XNYS | Financials | 1978-01-03 | 58.21
XOM | Exxon Mobil Corporation | XNYS | Energy | 1920-03-31 | 117.93
"""


def parse_ticker_table(table: str) -> list[tuple[str, ...]]:
    """Split a ``|``-separated table (one ticker per line) into stripped cells."""
    return [tuple(cell.strip() for cell in line.split("|")) for line in table.strip().splitlines()]


class Catalog:
    """Read-only lookups over the seed data. Codes and symbols are matched case-insensitively."""

    def __init__(self, exchanges: Iterable[Exchange], rows: Iterable[tuple[str, ...]]) -> None:
        self.exchanges: tuple[Exchange, ...] = tuple(sorted(exchanges, key=lambda ex: ex.code))
        self._exchanges = {ex.code: ex for ex in self.exchanges}
        tickers = [self._ticker(*row) for row in rows]
        self.tickers: tuple[Ticker, ...] = tuple(sorted(tickers, key=lambda t: t.symbol))
        self._tickers = {t.symbol: t for t in self.tickers}
        self.sectors: tuple[str, ...] = tuple(sorted({t.sector for t in self.tickers}))
        self._sectors = {sector.lower(): sector for sector in self.sectors}

    def _ticker(self, *row: str) -> Ticker:
        symbol, name, exchange, sector, listed_on, price = row
        return Ticker(
            symbol=symbol,
            name=name,
            exchange=exchange,
            currency=self._exchanges[exchange].currency,
            sector=sector,
            listed_on=date.fromisoformat(listed_on),
            price=Decimal(price),
        )

    def exchange(self, code: str) -> Exchange | None:
        """Look up an exchange by code (case-insensitive)."""
        return self._exchanges.get(code.upper())

    def ticker(self, symbol: str) -> Ticker | None:
        """Look up a ticker by symbol (case-insensitive)."""
        return self._tickers.get(symbol.upper())

    def exchange_of(self, ticker: Ticker) -> Exchange:
        """The exchange ``ticker`` is listed on."""
        return self._exchanges[ticker.exchange]

    def sector(self, name: str) -> str | None:
        """Return the canonical spelling of a sector name (case-insensitive), if it exists."""
        return self._sectors.get(name.lower())


def default_catalog() -> Catalog:
    """Build the catalog from the built-in seed data."""
    return Catalog(EXCHANGES, parse_ticker_table(_TICKER_TABLE))
