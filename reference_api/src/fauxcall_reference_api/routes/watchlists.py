"""Watchlists: stateful CRUD shared by every API key."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, Depends, Path, Response

from fauxcall_reference_api.errors import ApiError
from fauxcall_reference_api.routes.common import Ctx, document_api_key, error_responses
from fauxcall_reference_api.schemas import (
    WatchlistCreate,
    WatchlistOut,
    WatchlistPatch,
    WatchlistReplace,
)
from fauxcall_reference_api.views import watchlist_out

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fauxcall_reference_api.context import AppContext
    from fauxcall_reference_api.state import Watchlist

router = APIRouter(
    prefix="/v1",
    tags=["watchlists"],
    dependencies=[Depends(document_api_key)],
    responses=error_responses(401, 403, 429),
)

_LOCATION_HEADER: Final = {
    "Location": {
        "description": "Path of the new watchlist, e.g. /v1/watchlists/1001.",
        "schema": {"type": "string"},
    }
}


def canonical_symbols(context: AppContext, symbols: Iterable[str]) -> list[str]:
    """Canonical (upper-case) symbols, duplicates dropped, order kept; 400 on an unknown one."""
    result: list[str] = []
    for raw in symbols:
        ticker = context.catalog.ticker(raw)
        if ticker is None:
            raise ApiError(400, "unknown_symbol", f"Unknown symbol '{raw}'.", param="symbols")
        if ticker.symbol not in result:
            result.append(ticker.symbol)
    return result


def _watchlist_or_404(context: AppContext, watchlist_id: int) -> Watchlist:
    watchlist = context.watchlists.get(watchlist_id)
    if watchlist is None:
        raise ApiError(404, "watchlist_not_found", f"No watchlist with id {watchlist_id}.")
    return watchlist


@router.post(
    "/watchlists",
    status_code=201,
    summary="Create a watchlist",
    responses={201: {"headers": _LOCATION_HEADER}, **error_responses(400)},
)
async def create_watchlist(body: WatchlistCreate, ctx: Ctx, response: Response) -> WatchlistOut:
    """Create a watchlist; ids start at 1001. Responds 201 with a ``Location`` header."""
    symbols = canonical_symbols(ctx, body.symbols)
    watchlist = ctx.watchlists.create(body.name, symbols, ctx.now_ns())
    response.headers["Location"] = f"/v1/watchlists/{watchlist.id}"
    return watchlist_out(watchlist)


@router.get("/watchlists", summary="List watchlists")
async def list_watchlists(ctx: Ctx) -> list[WatchlistOut]:
    """Every watchlist, as a bare array sorted by id."""
    return [watchlist_out(watchlist) for watchlist in ctx.watchlists.all()]


@router.get(
    "/watchlists/{id}",
    summary="Get a watchlist",
    responses=error_responses(400, 404),
)
async def get_watchlist(
    watchlist_id: Annotated[int, Path(alias="id", description="Watchlist id.")], ctx: Ctx
) -> WatchlistOut:
    """One watchlist."""
    return watchlist_out(_watchlist_or_404(ctx, watchlist_id))


@router.put(
    "/watchlists/{id}",
    summary="Replace a watchlist",
    responses=error_responses(400, 404),
)
async def replace_watchlist(
    watchlist_id: Annotated[int, Path(alias="id", description="Watchlist id.")],
    body: WatchlistReplace,
    ctx: Ctx,
) -> WatchlistOut:
    """Replace name and symbols (both required)."""
    watchlist = _watchlist_or_404(ctx, watchlist_id)
    symbols = canonical_symbols(ctx, body.symbols)
    watchlist.name = body.name
    watchlist.symbols = symbols
    watchlist.updated_ns = ctx.now_ns()
    return watchlist_out(watchlist)


@router.patch(
    "/watchlists/{id}",
    summary="Update a watchlist",
    responses=error_responses(400, 404),
)
async def update_watchlist(
    watchlist_id: Annotated[int, Path(alias="id", description="Watchlist id.")],
    body: WatchlistPatch,
    ctx: Ctx,
) -> WatchlistOut:
    """Replace only the fields present in the body (``symbols`` is replaced, not appended)."""
    watchlist = _watchlist_or_404(ctx, watchlist_id)
    symbols = canonical_symbols(ctx, body.symbols) if body.symbols is not None else None
    if body.name is not None:
        watchlist.name = body.name
    if symbols is not None:
        watchlist.symbols = symbols
    watchlist.updated_ns = ctx.now_ns()
    return watchlist_out(watchlist)


@router.delete(
    "/watchlists/{id}",
    status_code=204,
    summary="Delete a watchlist",
    response_class=Response,
    responses=error_responses(400, 404),
)
async def delete_watchlist(
    watchlist_id: Annotated[int, Path(alias="id", description="Watchlist id.")], ctx: Ctx
) -> Response:
    """Delete a watchlist (204, empty body)."""
    if not ctx.watchlists.delete(watchlist_id):
        raise ApiError(404, "watchlist_not_found", f"No watchlist with id {watchlist_id}.")
    return Response(status_code=204)
