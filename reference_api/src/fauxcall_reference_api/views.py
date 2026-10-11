"""Domain objects to response models, shared by REST, WebSocket and MCP so they always agree."""

from __future__ import annotations

import base64
import binascii
import json
from typing import TYPE_CHECKING, Final

from fauxcall_reference_api import market
from fauxcall_reference_api.schemas import (
    ExchangeOut,
    MarketStatusOut,
    QuoteOut,
    SearchOut,
    SearchResult,
    SessionHours,
    TickerOut,
    UploadOut,
    WatchlistOut,
)
from fauxcall_reference_api.search import search
from fauxcall_reference_api.simulation import format_price
from fauxcall_reference_api.timefmt import iso_python, iso_z

if TYPE_CHECKING:
    from fauxcall_reference_api.catalog import Catalog, Exchange, Ticker
    from fauxcall_reference_api.state import Upload, Watchlist

QUOTE_DELAY_NS: Final = 1_500_000_000
"""While a market is open, ``as_of`` lags the clock by 1.5 s."""


def exchange_out(exchange: Exchange) -> ExchangeOut:
    """Render an exchange."""
    return ExchangeOut(
        code=exchange.code,
        name=exchange.name,
        mic=exchange.mic,
        timezone=exchange.timezone,
        currency=exchange.currency,
        sessions=[
            SessionHours(open=s.open.strftime("%H:%M"), close=s.close.strftime("%H:%M"))
            for s in exchange.sessions
        ],
    )


def ticker_out(ticker: Ticker) -> TickerOut:
    """Render a ticker."""
    return TickerOut(
        symbol=ticker.symbol,
        name=ticker.name,
        exchange=ticker.exchange,
        currency=ticker.currency,
        sector=ticker.sector,
        listed_on=ticker.listed_on,
    )


def quote_as_of(exchange: Exchange, now_ns: int) -> tuple[market.MarketState, int]:
    """``(state, as_of_ns)``: now - 1.5 s while open, else the most recent session close."""
    if market.current_session(exchange, now_ns) is not None:
        return "open", now_ns - QUOTE_DELAY_NS
    return "closed", market.last_close(exchange, now_ns)


def quote_out(catalog: Catalog, ticker: Ticker, now_ns: int) -> QuoteOut:
    """Render a ticker's quote at ``now_ns``."""
    state, as_of_ns = quote_as_of(catalog.exchange_of(ticker), now_ns)
    return QuoteOut(
        symbol=ticker.symbol,
        price=format_price(ticker.price),
        currency=ticker.currency,
        market_state=state,
        as_of=iso_z(as_of_ns, 6),
    )


def search_out(catalog: Catalog, query: str, limit: int) -> SearchOut:
    """Render search results."""
    hits = search(query, catalog.tickers, limit)
    return SearchOut(
        query=query,
        results=[
            SearchResult(symbol=hit.ticker.symbol, name=hit.ticker.name, score=hit.score)
            for hit in hits
        ],
    )


def market_status_out(exchange: Exchange, now_ns: int) -> MarketStatusOut:
    """Render an exchange's trading status at ``now_ns``."""
    session = market.current_session(exchange, now_ns)
    return MarketStatusOut(
        exchange=exchange.code,
        name=exchange.name,
        timezone=exchange.timezone,
        market_state="open" if session is not None else "closed",
        session_closes_at=iso_z(session.close_ns, 0) if session is not None else None,
        next_open=iso_z(market.next_open(exchange, now_ns), 0),
        last_close=iso_z(market.last_close(exchange, now_ns), 0),
    )


def watchlist_out(watchlist: Watchlist) -> WatchlistOut:
    """Render a watchlist."""
    return WatchlistOut(
        id=watchlist.id,
        name=watchlist.name,
        symbols=list(watchlist.symbols),
        created_at=iso_z(watchlist.created_ns, 3),
        updated_at=iso_z(watchlist.updated_ns, 3),
    )


def upload_out(upload: Upload, now_ns: int) -> UploadOut:
    """Render an upload as observed at ``now_ns`` (optional keys only when applicable)."""
    view = upload.view(now_ns)
    out = UploadOut(
        id=upload.id,
        status=view.status,
        filename=upload.filename,
        size=upload.size,
        label=upload.label,
        created_at=iso_python(upload.created_ns),
        progress=view.progress,
    )
    if view.completed_ns is not None:
        out.completed_at = iso_python(view.completed_ns)
    if view.error is not None:
        out.error = view.error
    return out


def encode_cursor(after_symbol: str) -> str:
    """base64url (no padding) of the compact JSON ``{"after":"<symbol>"}``."""
    raw = json.dumps({"after": after_symbol}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> str | None:
    """The ``after`` symbol of a cursor, or ``None`` if the cursor is malformed."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError):
        return None
    after = payload.get("after") if isinstance(payload, dict) else None
    return after if isinstance(after, str) else None
