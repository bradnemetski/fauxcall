"""Streaming: Server-Sent Events of simulated trades, and a WebSocket quote feed."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, Depends, Query
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from fauxcall_reference_api.errors import ApiError
from fauxcall_reference_api.routes.common import Ctx, document_api_key, error_responses
from fauxcall_reference_api.simulation import format_price, trade_walk
from fauxcall_reference_api.timefmt import iso_z
from fauxcall_reference_api.views import quote_out

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fauxcall_reference_api.catalog import Ticker
    from fauxcall_reference_api.context import AppContext

TRADE_INTERVAL_S: Final = 0.2
QUOTE_INTERVAL_S: Final = 0.25
QUOTE_ROUNDS: Final = 5
WS_CLOSE_UNAUTHORIZED: Final = 4401
WS_CLOSE_NORMAL: Final = 1000

router = APIRouter(
    prefix="/v1",
    tags=["streaming"],
    dependencies=[Depends(document_api_key)],
    responses=error_responses(401, 403, 429),
)
socket_router = APIRouter(prefix="/v1")


class EventStreamResponse(StreamingResponse):
    """``text/event-stream`` (Starlette appends ``; charset=utf-8``)."""

    media_type = "text/event-stream"


def _compact(payload: object) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


async def trade_events(context: AppContext, ticker: Ticker, count: int) -> AsyncIterator[str]:
    """SSE frames: the first immediately, then one every 200 ms; the stream then ends."""
    for trade in trade_walk(ticker.symbol, ticker.price, count):
        if trade.sequence > 1:
            await context.sleep(TRADE_INTERVAL_S)
        data = {
            "symbol": ticker.symbol,
            "price": format_price(trade.price),
            "size": trade.size,
            "time": iso_z(context.now_ns(), 3),
        }
        yield f"id: {trade.sequence}\nevent: trade\ndata: {_compact(data)}\n\n"


@router.get(
    "/stream/trades",
    summary="Stream simulated trades (SSE)",
    response_class=EventStreamResponse,
    responses={
        200: {
            "description": "count events (id: 1..count, event: trade, data: JSON), 200 ms apart.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **error_responses(400),
    },
)
async def stream_trades(
    ctx: Ctx,
    symbol: Annotated[str, Query(description="Ticker symbol (case-insensitive).")],
    count: Annotated[int, Query(ge=1, le=50, description="Number of events.")] = 10,
) -> EventStreamResponse:
    """A finite SSE stream of a deterministic trade walk around the ticker's quote price."""
    ticker = ctx.catalog.ticker(symbol)
    if ticker is None:
        raise ApiError(400, "unknown_symbol", f"Unknown symbol '{symbol}'.", param="symbol")
    return EventStreamResponse(
        trade_events(ctx, ticker, count), headers={"Cache-Control": "no-cache"}
    )


class _ProtocolError(Exception):
    """A client message the server answers with ``{"type": "error", ...}``."""


def _parse_subscription(context: AppContext, payload: dict[str, object]) -> list[Ticker]:
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise _ProtocolError("symbols must be a non-empty array of strings.")
    tickers: list[Ticker] = []
    for raw in symbols:
        if not isinstance(raw, str):
            raise _ProtocolError("symbols must be a non-empty array of strings.")
        ticker = context.catalog.ticker(raw)
        if ticker is None:
            raise _ProtocolError(f"Unknown symbol '{raw}'.")
        if ticker not in tickers:
            tickers.append(ticker)
    return tickers


def parse_client_message(context: AppContext, text: str | None) -> list[Ticker] | None:
    """Validate one client message: ``None`` for ping, the tickers for subscribe."""
    if text is None:
        raise _ProtocolError("Binary messages are not supported.")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise _ProtocolError("Invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise _ProtocolError("Messages must be JSON objects.")
    action = payload.get("action")
    if action == "ping":
        return None
    if action == "subscribe":
        return _parse_subscription(context, payload)
    raise _ProtocolError(f"Unknown action: {_compact(action)}.")


async def _stream_quotes(websocket: WebSocket, context: AppContext, tickers: list[Ticker]) -> None:
    await websocket.send_json({"type": "subscribed", "symbols": [t.symbol for t in tickers]})
    for round_number in range(1, QUOTE_ROUNDS + 1):
        if round_number > 1:
            await context.sleep(QUOTE_INTERVAL_S)
        now_ns = context.now_ns()
        for ticker in tickers:
            quote = quote_out(context.catalog, ticker, now_ns)
            await websocket.send_json(
                {
                    "type": "quote",
                    "symbol": quote.symbol,
                    "price": quote.price,
                    "as_of": quote.as_of,
                }
            )
    await websocket.send_json({"type": "done"})
    await websocket.close(code=WS_CLOSE_NORMAL)


def _rejection_reason(context: AppContext, api_key: str | None) -> str | None:
    if not api_key:
        return "Missing api_key query parameter."
    access = context.settings.api_keys.get(api_key)
    if access is None:
        return "Invalid API key."
    if access == "revoked":
        return "This API key has been revoked."
    return None


async def _session(websocket: WebSocket, context: AppContext) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        try:
            tickers = parse_client_message(context, message.get("text"))
        except _ProtocolError as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            continue
        if tickers is None:
            await websocket.send_json({"type": "pong"})
            continue
        await _stream_quotes(websocket, context, tickers)
        return


@socket_router.websocket("/ws/quotes")
async def quotes_socket(websocket: WebSocket, ctx: Ctx) -> None:
    """Quote feed. Auth: ``?api_key=``; a bad key is accepted then closed with code 4401."""
    await websocket.accept()
    reason = _rejection_reason(ctx, websocket.query_params.get("api_key"))
    if reason is not None:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason=reason)
        return
    with contextlib.suppress(WebSocketDisconnect):
        await _session(websocket, ctx)
