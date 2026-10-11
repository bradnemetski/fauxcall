from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from fauxcall_reference_api.catalog import default_catalog
from fauxcall_reference_api.ratelimit import RateLimiter
from fauxcall_reference_api.search import score, search, tokenize
from fauxcall_reference_api.simulation import (
    digest_int,
    format_price,
    latency_ms,
    trade_walk,
)

from .helpers import SECOND

CATALOG = default_catalog()


def ticker(symbol: str):  # type: ignore[no-untyped-def]  # noqa: ANN201
    found = CATALOG.ticker(symbol)
    assert found is not None
    return found


# -- simulation --------------------------------------------------------------------------------


def test_digest_int_is_the_sha256_prefix() -> None:
    expected = int.from_bytes(hashlib.sha256(b"abc").digest()[:8], "big")
    assert digest_int("abc") == expected


def test_latency_is_deterministic_per_method_and_path() -> None:
    value = latency_ms("GET", "/v1/tickers")
    assert value == 15 + digest_int("refapi-latency:GET /v1/tickers") % 46
    assert value == latency_ms("GET", "/v1/tickers")
    samples = {latency_ms("GET", f"/v1/tickers/{t.symbol}") for t in CATALOG.tickers}
    assert min(samples) >= 15
    assert max(samples) <= 60
    assert len(samples) > 10  # spread across the range


def test_format_price() -> None:
    assert format_price(Decimal("2741.5")) == "2741.50"
    assert format_price(Decimal("1.005")) == "1.01"


def test_trade_walk_is_deterministic_and_bounded() -> None:
    walk = trade_walk("AAPL", Decimal("227.48"), 50)
    assert walk == trade_walk("AAPL", Decimal("227.48"), 50)
    assert [t.sequence for t in walk] == list(range(1, 51))
    previous = Decimal("227.48")
    for trade in walk:
        digest = digest_int(f"refapi-trade:AAPL:{trade.sequence}")
        step = Decimal(digest % 21 - 10) / 10_000
        expected = (previous * (1 + step)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
        assert trade.price == expected
        assert trade.size == 1 + (digest >> 16) % 1000
        assert abs(trade.price - previous) <= previous * Decimal("0.001") + Decimal("0.005")
        previous = trade.price
    assert trade_walk("AAPL", Decimal("227.48"), 3) == walk[:3]


# -- search ------------------------------------------------------------------------------------


def test_tokenize() -> None:
    assert tokenize("  Moët & 7203.T  ") == ["mo", "t", "7203", "t"]
    assert tokenize("!!") == []


@pytest.mark.parametrize(
    ("query", "symbol", "expected"),
    [
        ("aapl", "AAPL", 1.0),  # symbol
        ("7203", "7203.T", 1.0),  # symbol root
        ("shel", "SHEL.L", 1.0),
        ("aa", "AAPL", 0.9),  # symbol prefix (2+ chars)
        ("apple", "AAPL", 0.8),  # name word
        ("micro", "MSFT", 0.6),  # name-word prefix (3+ chars)
        ("technology", "MSFT", 0.5),  # sector word
        ("services", "META", 0.5),  # word of a multi-word sector
        ("car", "TSLA", 0.5),  # synonym
        ("chips", "NVDA", 0.5),
        ("bank", "BAC", 0.8),  # name word beats synonym
        ("bank", "JPM", 0.5),
        ("apple bank", "AAPL", 0.4),  # mean of 0.8 and 0
        ("apple inc tech", "AAPL", 0.7),  # (0.8 + 0.8 + 0.5) / 3
        ("apple tech zzz", "AAPL", 0.433),  # 1.3 / 3 = 0.4333...
        ("tech zz zz zz zz zz zz zz", "AAPL", 0.063),  # 0.5 / 8 = 0.0625, rounded half-up
        ("zzz", "AAPL", 0.0),
        ("a", "AAPL", 0.0),  # 1-char tokens never prefix-match
    ],
)
def test_score(query: str, symbol: str, expected: float) -> None:
    assert score(tokenize(query), ticker(symbol)) == expected


def test_score_of_no_tokens_is_zero() -> None:
    assert score([], ticker("AAPL")) == 0.0


def test_search_orders_by_score_then_symbol_and_limits() -> None:
    hits = search("bank", CATALOG.tickers)
    assert [(h.ticker.symbol, h.score) for h in hits] == [
        ("BAC", 0.8),
        ("0005.HK", 0.5),
        ("0388.HK", 0.5),
        ("8306.T", 0.5),
        ("BARC.L", 0.5),
        ("BNP.PA", 0.5),
        ("GS", 0.5),
        ("HSBA.L", 0.5),
        ("JPM", 0.5),
        ("PYPL", 0.5),
    ]
    assert len(search("bank", CATALOG.tickers, 3)) == 3
    assert search("!!", CATALOG.tickers) == []
    assert search("qqqq", CATALOG.tickers) == []


# -- rate limiting -----------------------------------------------------------------------------


def test_rate_limiter_fixed_window() -> None:
    limiter = RateLimiter(limit=3, window_seconds=10)
    start = 1_000 * SECOND + 300_000_000  # window starts at 1000.3 s
    first = limiter.hit("k", start)
    assert (first.allowed, first.remaining, first.reset_epoch_s) == (True, 2, 1011)
    assert first.headers() == {
        "X-RateLimit-Limit": "3",
        "X-RateLimit-Remaining": "2",
        "X-RateLimit-Reset": "1011",
    }
    assert limiter.hit("k", start + SECOND).remaining == 1
    assert limiter.hit("k", start + 2 * SECOND).remaining == 0
    rejected = limiter.hit("k", start + 4_500_000_000)
    assert not rejected.allowed
    assert rejected.remaining == 0
    assert rejected.retry_after_s == 6  # ceil(1010.3 - 1004.8)
    assert rejected.headers()["Retry-After"] == "6"
    assert limiter.hit("k", start + 5 * SECOND).retry_after_s == 5  # exactly 5 s left
    # Other keys have their own windows.
    assert limiter.hit("other", start + 5 * SECOND).allowed
    # Just before the end: still limited, Retry-After never below 1.
    late = limiter.hit("k", start + 10 * SECOND - 1)
    assert (late.allowed, late.retry_after_s) == (False, 1)
    # At the end: a new window starting now.
    fresh = limiter.hit("k", start + 10 * SECOND)
    assert (fresh.allowed, fresh.remaining, fresh.reset_epoch_s) == (True, 2, 1021)
