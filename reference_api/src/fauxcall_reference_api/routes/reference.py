"""Read-only reference data: exchanges, tickers, quotes, search and server time."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query

from fauxcall_reference_api.clock import NS_PER_MS
from fauxcall_reference_api.errors import ApiError
from fauxcall_reference_api.routes.common import (
    Ctx,
    document_api_key,
    error_responses,
    ticker_or_404,
)
from fauxcall_reference_api.schemas import (
    ExchangeOut,
    QuoteOut,
    SearchOut,
    ServerTimeOut,
    TickerOut,
    TickerPage,
)
from fauxcall_reference_api.search import MAX_RESULTS
from fauxcall_reference_api.timefmt import iso_z
from fauxcall_reference_api.views import (
    decode_cursor,
    encode_cursor,
    exchange_out,
    quote_out,
    search_out,
    ticker_out,
)

if TYPE_CHECKING:
    from fauxcall_reference_api.catalog import Ticker
    from fauxcall_reference_api.context import AppContext

router = APIRouter(
    prefix="/v1",
    dependencies=[Depends(document_api_key)],
    responses=error_responses(401, 403, 429),
)


@router.get("/exchanges", tags=["exchanges"], summary="List exchanges")
async def list_exchanges(ctx: Ctx) -> list[ExchangeOut]:
    """Every exchange, as a bare array sorted by code."""
    return [exchange_out(exchange) for exchange in ctx.catalog.exchanges]


@router.get(
    "/exchanges/{code}",
    tags=["exchanges"],
    summary="Get an exchange",
    responses=error_responses(404),
)
async def get_exchange(code: str, ctx: Ctx) -> ExchangeOut:
    """One exchange by code (case-insensitive)."""
    exchange = ctx.catalog.exchange(code)
    if exchange is None:
        raise ApiError(404, "exchange_not_found", f"No exchange with code '{code.upper()}'.")
    return exchange_out(exchange)


@router.get(
    "/tickers",
    tags=["tickers"],
    summary="List tickers",
    responses=error_responses(400),
)
async def list_tickers(
    ctx: Ctx,
    exchange: Annotated[
        str | None, Query(description="Exchange code filter (case-insensitive).")
    ] = None,
    sector: Annotated[str | None, Query(description="Sector filter (case-insensitive).")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size.")] = 20,
    cursor: Annotated[str | None, Query(description="next_cursor of the previous page.")] = None,
) -> TickerPage:
    """Tickers sorted by symbol, filtered, paginated with an opaque cursor.

    Empty filter or cursor values are ignored. Unknown exchange or sector: 400
    ``invalid_parameter``; malformed or unknown cursor: 400 ``invalid_cursor``.
    """
    tickers = list(ctx.catalog.tickers)
    if exchange:
        found = ctx.catalog.exchange(exchange)
        if found is None:
            raise ApiError(
                400, "invalid_parameter", f"Unknown exchange '{exchange}'.", param="exchange"
            )
        tickers = [t for t in tickers if t.exchange == found.code]
    if sector:
        canonical = ctx.catalog.sector(sector)
        if canonical is None:
            raise ApiError(400, "invalid_parameter", f"Unknown sector '{sector}'.", param="sector")
        tickers = [t for t in tickers if t.sector == canonical]
    total = len(tickers)
    if cursor:
        after = _cursor_ticker(ctx, cursor)
        tickers = [t for t in tickers if t.symbol > after.symbol]
    page = tickers[:limit]
    next_cursor = encode_cursor(page[-1].symbol) if len(tickers) > limit else None
    return TickerPage(data=[ticker_out(t) for t in page], next_cursor=next_cursor, total=total)


def _cursor_ticker(context: AppContext, cursor: str) -> Ticker:
    after = decode_cursor(cursor)
    ticker = context.catalog.ticker(after) if after is not None else None
    if ticker is None:
        raise ApiError(400, "invalid_cursor", "Invalid or unknown cursor.", param="cursor")
    return ticker


@router.get(
    "/tickers/{symbol}",
    tags=["tickers"],
    summary="Get a ticker",
    responses=error_responses(404),
)
async def get_ticker(symbol: str, ctx: Ctx) -> TickerOut:
    """One ticker by symbol (case-insensitive; the response uses the canonical upper case)."""
    return ticker_out(ticker_or_404(ctx, symbol))


@router.get(
    "/tickers/{symbol}/quote",
    tags=["quotes"],
    summary="Get a quote",
    responses=error_responses(404),
)
async def get_quote(symbol: str, ctx: Ctx) -> QuoteOut:
    """The ticker's static price; ``as_of`` follows its exchange's trading sessions."""
    return quote_out(ctx.catalog, ticker_or_404(ctx, symbol), ctx.now_ns())


@router.get("/search", tags=["search"], summary="Search tickers", responses=error_responses(400))
async def search_tickers(
    ctx: Ctx,
    q: Annotated[
        str, Query(min_length=2, max_length=64, description="Free text, 2-64 characters.")
    ],
) -> SearchOut:
    """Token search over symbol, name and sector (with synonyms); top 10 by score, then symbol."""
    return search_out(ctx.catalog, q, MAX_RESULTS)


@router.get("/time", tags=["time"], summary="Server time")
async def server_time(ctx: Ctx) -> ServerTimeOut:
    """The actual current time (``utc`` has 7 fractional digits, .NET style)."""
    now_ns = ctx.now_ns()
    return ServerTimeOut(utc=iso_z(now_ns, 7), unix_ms=now_ns // NS_PER_MS, zone="UTC")
