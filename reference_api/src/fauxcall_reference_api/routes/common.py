"""Dependencies and OpenAPI helpers shared by the routers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final, cast

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from starlette.requests import HTTPConnection  # noqa: TC002 - FastAPI resolves it at runtime

from fauxcall_reference_api.context import AppContext
from fauxcall_reference_api.errors import ApiError, ErrorEnvelope
from fauxcall_reference_api.gateway import API_KEY_HEADER

if TYPE_CHECKING:
    from fauxcall_reference_api.catalog import Ticker

API_KEY_SCHEME: Final = APIKeyHeader(
    name=API_KEY_HEADER,
    scheme_name="ApiKeyAuth",
    description=(
        "Keys: demo-key (full access), readonly-key (GET only), revoked-key (always 403). "
        "Missing or unknown key: 401."
    ),
    auto_error=False,
)

_ERROR_DESCRIPTIONS: Final[dict[int, str]] = {
    400: "Invalid request (bad parameter, body, cursor or symbol).",
    401: "Missing or unknown API key.",
    403: "Revoked API key, or a write with the read-only key.",
    404: "Resource not found.",
    409: "The upload is not ready (status is not done).",
    413: "Uploaded file larger than 1 MiB.",
    429: "Rate limit exceeded (see Retry-After).",
}


def get_context(connection: HTTPConnection) -> AppContext:
    """The app's :class:`AppContext`."""
    return cast("AppContext", connection.app.state.context)


Ctx = Annotated[AppContext, Depends(get_context)]


async def document_api_key(
    _api_key: Annotated[str | None, Security(API_KEY_SCHEME)] = None,
) -> None:
    """Declare the API-key scheme in OpenAPI; the gateway middleware enforces it."""


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries using the error envelope."""
    return {
        status: {"model": ErrorEnvelope, "description": _ERROR_DESCRIPTIONS[status]}
        for status in statuses
    }


def ticker_or_404(context: AppContext, symbol: str) -> Ticker:
    """Look a ticker up (case-insensitively) or fail with 404 ``ticker_not_found``."""
    ticker = context.catalog.ticker(symbol)
    if ticker is None:
        raise ApiError(404, "ticker_not_found", f"No ticker with symbol '{symbol.upper()}'.")
    return ticker
