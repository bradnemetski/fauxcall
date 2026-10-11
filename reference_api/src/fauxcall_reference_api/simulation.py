"""Deterministic pseudo-randomness: per-path latency and the simulated trade walk.

Everything derives from :func:`digest_int` (SHA-256), so values are identical across runs,
machines and Python versions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

LATENCY_MIN_MS: Final = 15
LATENCY_MAX_MS: Final = 60
CENT: Final = Decimal("0.01")
_BASIS_POINTS: Final = 10_000
_STEP_SPAN: Final = 21  # steps of -10..+10 basis points
_STEP_OFFSET: Final = 10
_MAX_TRADE_SIZE: Final = 1_000


def digest_int(text: str) -> int:
    """The first 8 bytes of SHA-256(UTF-8 ``text``) as a big-endian unsigned integer."""
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


def latency_ms(method: str, path: str) -> int:
    """The fixed artificial latency of ``method path``: an integer in [15, 60] milliseconds."""
    span = LATENCY_MAX_MS - LATENCY_MIN_MS + 1
    return LATENCY_MIN_MS + digest_int(f"refapi-latency:{method} {path}") % span


def format_price(price: Decimal) -> str:
    """Render a price with exactly two decimals (half-up)."""
    return str(price.quantize(CENT, rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class Trade:
    """One simulated trade: the n-th step of a symbol's walk."""

    sequence: int
    price: Decimal
    size: int


def trade_walk(symbol: str, start: Decimal, count: int) -> list[Trade]:
    """The first ``count`` trades of ``symbol``'s deterministic random walk from ``start``.

    Trade ``n`` (1-based) uses ``h = digest_int(f"refapi-trade:{symbol}:{n}")``: the price moves
    by ``(h % 21) - 10`` basis points from the previous trade's price (rounded half-up to cents)
    and the size is ``1 + (h >> 16) % 1000``.
    """
    trades: list[Trade] = []
    price = start
    for sequence in range(1, count + 1):
        digest = digest_int(f"refapi-trade:{symbol}:{sequence}")
        step = digest % _STEP_SPAN - _STEP_OFFSET
        price = (price * (_BASIS_POINTS + step) / _BASIS_POINTS).quantize(CENT, ROUND_HALF_UP)
        trades.append(Trade(sequence, price, 1 + (digest >> 16) % _MAX_TRADE_SIZE))
    return trades
