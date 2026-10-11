"""Ticker search: token matching on symbol, name and sector, plus a synonym table.

For each query token the ticker earns the best of:

====  ==========================================================================
1.0   token equals the symbol's root: the part before the first ``.`` (``7203`` for ``7203.T``)
0.9   token (2+ chars) is a prefix of the symbol's root
0.8   token equals a word of the name
0.6   token (3+ chars) is a prefix of a word of the name
0.5   token equals a word of the sector, or is a synonym of the sector
0.0   otherwise
====  ==========================================================================

The ticker's score is the mean over all query tokens, rounded half-up to 3 decimals. Tickers
scoring 0 are dropped; the rest are ordered by score (descending) then symbol (ascending).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final

from fauxcall_reference_api.catalog import SYNONYMS

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from fauxcall_reference_api.catalog import Ticker

MAX_RESULTS: Final = 10
_TOKEN_RE: Final = re.compile(r"[a-z0-9]+")
_SCORE_QUANTUM: Final = Decimal("0.001")
_MIN_SYMBOL_PREFIX: Final = 2
_MIN_NAME_PREFIX: Final = 3
# Scores in tenths so sums stay exact.
_EXACT_SYMBOL: Final = 10
_SYMBOL_PREFIX: Final = 9
_NAME_WORD: Final = 8
_NAME_PREFIX: Final = 6
_SECTOR: Final = 5


def tokenize(text: str) -> list[str]:
    """Lower-case ``text`` and split it into runs of ASCII letters and digits."""
    return _TOKEN_RE.findall(text.lower())


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A matching ticker and its score."""

    ticker: Ticker
    score: float


def _token_tenths(token: str, ticker: Ticker, synonyms: Mapping[str, str]) -> int:
    root = ticker.symbol.lower().split(".", 1)[0]
    if token == root:
        return _EXACT_SYMBOL
    if len(token) >= _MIN_SYMBOL_PREFIX and root.startswith(token):
        return _SYMBOL_PREFIX
    name_words = tokenize(ticker.name)
    if token in name_words:
        return _NAME_WORD
    if len(token) >= _MIN_NAME_PREFIX and any(word.startswith(token) for word in name_words):
        return _NAME_PREFIX
    if token in tokenize(ticker.sector) or synonyms.get(token) == ticker.sector:
        return _SECTOR
    return 0


def score(tokens: list[str], ticker: Ticker, synonyms: Mapping[str, str] = SYNONYMS) -> float:
    """Mean per-token score of ``ticker`` for ``tokens``, rounded half-up to 3 decimals."""
    if not tokens:
        return 0.0
    tenths = sum(_token_tenths(token, ticker, synonyms) for token in tokens)
    mean = Decimal(tenths) / Decimal(10 * len(tokens))
    return float(mean.quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP))


def search(query: str, tickers: Iterable[Ticker], limit: int = MAX_RESULTS) -> list[SearchHit]:
    """The best ``limit`` matches for ``query`` (score descending, then symbol ascending)."""
    tokens = tokenize(query)
    hits = [SearchHit(ticker, score(tokens, ticker)) for ticker in tickers]
    ranked = sorted(
        (hit for hit in hits if hit.score > 0), key=lambda h: (-h.score, h.ticker.symbol)
    )
    return ranked[:limit]
