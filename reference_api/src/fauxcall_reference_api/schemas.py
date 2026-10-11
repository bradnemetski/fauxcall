"""Request and response bodies (pydantic). Field order is the JSON key order."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator
from pydantic.json_schema import SkipJsonSchema

WatchlistName = Annotated[str, StringConstraints(min_length=1, max_length=50)]
MarketStateField = Literal["open", "closed"]


class SessionHours(BaseModel):
    """One trading session in exchange-local time (``HH:MM``)."""

    open: str = Field(examples=["09:30"])
    close: str = Field(examples=["16:00"])


class ExchangeOut(BaseModel):
    """An exchange."""

    code: str = Field(examples=["XNYS"])
    name: str = Field(examples=["New York Stock Exchange"])
    mic: str = Field(examples=["XNYS"])
    timezone: str = Field(examples=["America/New_York"])
    currency: str = Field(examples=["USD"])
    sessions: list[SessionHours]


class TickerOut(BaseModel):
    """A listed instrument. ``exchange`` is an exchange ``code``."""

    symbol: str = Field(examples=["AAPL"])
    name: str = Field(examples=["Apple Inc."])
    exchange: str = Field(examples=["XNAS"])
    currency: str = Field(examples=["USD"])
    sector: str = Field(examples=["Technology"])
    listed_on: date


class TickerPage(BaseModel):
    """One page of tickers sorted by symbol."""

    data: list[TickerOut]
    next_cursor: str | None = Field(
        description='base64url (unpadded) of the compact JSON {"after":"<last symbol>"}; '
        "null on the last page."
    )
    total: int = Field(description="Number of tickers matching the filters (all pages).")


class QuoteOut(BaseModel):
    """A quote. The price never changes; ``as_of`` follows the exchange's trading sessions."""

    symbol: str
    price: str = Field(description="Decimal string with exactly 2 decimals.", examples=["227.48"])
    currency: str
    market_state: MarketStateField
    as_of: str = Field(
        description="ISO-8601 UTC with 6 fractional digits and Z. While open: now - 1.5 s; "
        "while closed: the most recent session close.",
        examples=["2026-10-09T20:00:00.000000Z"],
    )


class ServerTimeOut(BaseModel):
    """The server's current time."""

    utc: str = Field(examples=["2026-10-10T12:00:00.1234567Z"])
    unix_ms: int
    zone: Literal["UTC"]


class SearchResult(BaseModel):
    """A search hit."""

    symbol: str
    name: str
    score: float = Field(ge=0, le=1, description="0-1, rounded half-up to 3 decimals.")


class SearchOut(BaseModel):
    """Search results: at most 10, by score (descending) then symbol."""

    query: str
    results: list[SearchResult]


class WatchlistCreate(BaseModel):
    """Create a watchlist. Symbols are case-insensitive; duplicates are dropped."""

    name: WatchlistName
    symbols: list[str] = Field(default_factory=list)


class WatchlistReplace(BaseModel):
    """Replace a watchlist (both fields required)."""

    name: WatchlistName
    symbols: list[str]


class WatchlistPatch(BaseModel):
    """Change some fields of a watchlist; absent fields are kept, ``null`` is rejected."""

    name: WatchlistName | SkipJsonSchema[None] = None
    symbols: list[str] | SkipJsonSchema[None] = None

    @field_validator("name", "symbols", mode="before")
    @classmethod
    def _reject_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("must not be null")
        return value


class WatchlistOut(BaseModel):
    """A watchlist. Timestamps: ISO-8601 UTC with milliseconds and Z."""

    id: int = Field(examples=[1001])
    name: str
    symbols: list[str]
    created_at: str = Field(examples=["2026-10-10T12:00:00.123Z"])
    updated_at: str = Field(examples=["2026-10-10T12:00:00.123Z"])


class UploadOut(BaseModel):
    """An upload job. ``completed_at`` and ``error`` are present only when applicable."""

    id: str = Field(description="UUID4.")
    status: Literal["queued", "processing", "done", "failed"]
    filename: str
    size: int = Field(description="File size in bytes.")
    label: str | None
    created_at: str = Field(
        description="Python isoformat() of the UTC instant: YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00.",
        examples=["2026-10-10T12:00:00.123456+00:00"],
    )
    progress: int = Field(ge=0, le=100)
    completed_at: str | SkipJsonSchema[None] = Field(
        default=None, description="Present once terminal: created_at + 8 s (same format)."
    )
    error: Literal["corrupt_file"] | SkipJsonSchema[None] = Field(
        default=None, description="Present only when status is failed."
    )


class HealthOut(BaseModel):
    """Liveness."""

    status: Literal["ok"]


class MarketStatusOut(BaseModel):
    """An exchange's trading status (MCP ``market_status``). Instants: ISO-8601 UTC, seconds, Z."""

    exchange: str
    name: str
    timezone: str
    market_state: MarketStateField
    session_closes_at: str | None = Field(description="When the current session ends, if open.")
    next_open: str = Field(description="The next session open strictly after now.")
    last_close: str = Field(description="The most recent session close at or before now.")
