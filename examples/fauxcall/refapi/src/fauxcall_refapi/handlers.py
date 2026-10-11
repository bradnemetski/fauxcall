"""Custom behavior of the refapi fake: yours to edit, ``fauxcall scaffold`` never overwrites it.

Every rule here was observed in ``data/recordings`` (exchange ids in the comments) unless a comment
says it comes from the documentation. All endpoints read and write the one consistent world
(``ctx.world``), so a ticker's exchange exists, a watchlist only holds real symbols and an upload is
visible to its status, list and content endpoints.

Validation order follows the real server: its framework validates declared parameters first (in
declaration order: missing, then type/range), then the application checks existence (unknown
exchange, sector, cursor or symbol). Writes with the read-only key are rejected before anything
else.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from typing import TYPE_CHECKING, Final, cast

from fastapi import APIRouter, Request, Response
from fauxcall.runtime import runtime_of
from starlette.datastructures import UploadFile
from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
    from pathlib import Path

    from fastapi import WebSocket
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue
    from fauxcall.world.store import SessionWorld

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

NS: Final = 1_000_000_000
MS: Final = 1_000_000
READ_ONLY_USER: Final = "readonly"
"""Identity name of the ``readonly-key`` user (see ``fauxcall.toml``): GET only."""

# -- shared helpers ----------------------------------------------------------------------------


def api_error(
    ctx: RequestContext,
    status: int,
    code: str,
    message: str,
    *,
    param: str | None = None,
) -> Response:
    """The real error envelope: ``{"error": {code, message, request_id[, param]}}``."""
    error: dict[str, JsonValue] = {"code": code, "message": message, "request_id": _uuid4(ctx)}
    if param is not None:
        error["param"] = param
    return ctx.respond({"error": error}, status=status)


def _uuid4(ctx: RequestContext) -> str:
    """A UUID4 from the request's seeded generator (reproducible per session and request)."""
    return str(uuid.UUID(int=ctx.rng.getrandbits(128), version=4))


def _last(ctx: RequestContext, name: str) -> str | None:
    """The last value of a query parameter (the real API lets the last repetition win)."""
    values = ctx.request.query_params.getlist(name)
    return values[-1] if values else None


_ZERO_FRACTION: Final = re.compile(r"(\s*[+-]?[0-9_]+)\.0*\s*")


def lax_int(text: str) -> int | None:
    """Parse an integer the way the real API's validator does, or ``None``.

    Accepts surrounding whitespace, a sign, leading zeros, ``_`` separators and a zero fraction
    (``" 5"``, ``"+5"``, ``"05"``, ``"1_001"``, ``"1001.0"``: 000087-000089, 000475, 000717);
    rejects ``""``, ``"2.5"``, ``"1e3"``, ``"inf"`` (000064, 000065, 000713, 000714, 000718).
    """
    zero = _ZERO_FRACTION.fullmatch(text)
    candidate = zero.group(1) if zero else text
    try:
        return int(candidate)
    except ValueError:
        return None


def iso_ms_z(ns: int) -> str:
    """``2026-10-11T06:22:38.613Z`` (watchlists, trades)."""
    moment = datetime.fromtimestamp(ns // NS, tz=UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{(ns % NS) // MS:03d}Z"


def iso_us_z(ns: int) -> str:
    """``2026-10-09T20:00:00.000000Z`` (quote ``as_of``)."""
    moment = datetime.fromtimestamp(ns // NS, tz=UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{(ns % NS) // 1000:06d}Z"


def python_isoformat(ns: int) -> str:
    """Python ``datetime.isoformat()`` of a UTC instant (uploads: ``…:11.465149+00:00``)."""
    moment = datetime.fromtimestamp(ns // NS, tz=UTC).replace(microsecond=(ns % NS) // 1000)
    return moment.isoformat()


def parse_isoformat_ns(text: str) -> int:
    """Inverse of :func:`python_isoformat` (microsecond precision)."""
    moment = datetime.fromisoformat(text)
    seconds = int(moment.replace(microsecond=0).timestamp())
    return seconds * NS + moment.microsecond * 1000


def _record(world: SessionWorld, collection: str, key: JsonValue) -> JsonObject | None:
    found = world.get(collection, key)
    if found is None or not isinstance(found.record, dict):
        return None
    return found.record


def _records(world: SessionWorld, collection: str) -> list[JsonObject]:
    return [r.record for r in world.list(collection) if isinstance(r.record, dict)]


def _read_only(ctx: RequestContext) -> Response | None:
    """403 ``read_only_key`` for writes with the read-only key (000465, 000499, 000501, 000675)."""
    if ctx.identity is not None and ctx.identity.name == READ_ONLY_USER:
        method = ctx.request.scope.get("method", "POST")
        return api_error(
            ctx,
            403,
            "read_only_key",
            f"This API key is read-only; {method} requests are not allowed.",
        )
    return None


def _invalid(ctx: RequestContext, param: str, rule: str) -> Response:
    return api_error(
        ctx,
        400,
        "invalid_parameter",
        f"Invalid value for parameter '{param}': {rule}.",
        param=param,
    )


def _missing(ctx: RequestContext, param: str) -> Response:
    return api_error(
        ctx, 400, "missing_parameter", f"Missing required parameter '{param}'.", param=param
    )


# -- exchanges ---------------------------------------------------------------------------------


async def get_exchange(ctx: RequestContext) -> Response:
    """One exchange, case-insensitive; 404 echoes the upper-cased code (000022-000027)."""
    code = (ctx.path_param("code") or "").upper()
    record = _record(ctx.world, "exchanges", code)
    if record is None:
        return api_error(ctx, 404, "exchange_not_found", f"No exchange with code '{code}'.")
    return ctx.respond(record)


# -- tickers -----------------------------------------------------------------------------------


def _ticker(ctx: RequestContext, raw: str) -> JsonObject | None:
    return _record(ctx.world, "tickers", raw.upper())


def _ticker_not_found(ctx: RequestContext, symbol: str) -> Response:
    return api_error(ctx, 404, "ticker_not_found", f"No ticker with symbol '{symbol}'.")


def encode_cursor(symbol: str) -> str:
    """Unpadded base64url of the compact JSON ``{"after":"<symbol>"}`` (000034-000039)."""
    raw = json.dumps({"after": symbol}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> str | None:
    """The ``after`` symbol of a cursor, or ``None`` when it is malformed.

    Padding, standard-alphabet base64, JSON whitespace and extra keys are tolerated
    (000070, 000084-000086); ``after`` must be a string (000082, 000083, 000072).
    """
    padded = token + "=" * (-len(token) % 4)
    try:
        document = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    after = document.get("after") if isinstance(document, dict) else None
    return after if isinstance(after, str) else None


async def list_tickers(ctx: RequestContext) -> Response:
    """Filtered, cursor-paginated tickers sorted by symbol (000031-000098)."""
    limit = 20
    limit_raw = _last(ctx, "limit")
    if limit_raw is not None:
        parsed = lax_int(limit_raw)
        if parsed is None or not 1 <= parsed <= 100:  # noqa: PLR2004
            return _invalid(ctx, "limit", "must be an integer between 1 and 100")
        limit = parsed
    tickers = sorted(_records(ctx.world, "tickers"), key=lambda t: str(t["symbol"]))
    exchange = _last(ctx, "exchange")
    if exchange:
        if _record(ctx.world, "exchanges", exchange.upper()) is None:
            return api_error(
                ctx,
                400,
                "invalid_parameter",
                f"Unknown exchange '{exchange}'.",
                param="exchange",
            )
        tickers = [t for t in tickers if str(t["exchange"]) == exchange.upper()]
    sector = _last(ctx, "sector")
    if sector:
        sectors = {str(t["sector"]).lower() for t in _records(ctx.world, "tickers")}
        if sector.lower() not in sectors:
            return api_error(
                ctx, 400, "invalid_parameter", f"Unknown sector '{sector}'.", param="sector"
            )
        tickers = [t for t in tickers if str(t["sector"]).lower() == sector.lower()]
    total = len(tickers)
    cursor = _last(ctx, "cursor")
    if cursor:
        after = decode_cursor(cursor)
        if after is None or _ticker(ctx, after) is None:
            return api_error(
                ctx, 400, "invalid_cursor", "Invalid or unknown cursor.", param="cursor"
            )
        # A symbol the filters exclude still marks a place in the sorted list (000080).
        tickers = [t for t in tickers if str(t["symbol"]) > after.upper()]
    page = tickers[:limit]
    next_cursor = encode_cursor(str(page[-1]["symbol"])) if len(tickers) > limit else None
    return ctx.respond({"data": list(page), "next_cursor": next_cursor, "total": total})


async def get_ticker(ctx: RequestContext) -> Response:
    """One ticker, case-insensitive; any unknown symbol is a 404 (000100-000122)."""
    raw = ctx.path_param("symbol") or ""
    record = _ticker(ctx, raw)
    if record is None:
        return _ticker_not_found(ctx, raw.upper())
    return ctx.respond(record)


# -- quotes ------------------------------------------------------------------------------------

QUOTE_LAG_NS: Final = 1_500_000_000
"""While the market is open ``as_of`` is now - 1.5 s (QuoteOut.as_of documentation)."""


async def get_quote(ctx: RequestContext) -> Response:
    """Static price; ``market_state``/``as_of`` follow the exchange's sessions (000127-000199)."""
    raw = ctx.path_param("symbol") or ""
    ticker = _ticker(ctx, raw)
    if ticker is None:
        return _ticker_not_found(ctx, raw.upper())
    symbol = str(ticker["symbol"])
    quote = _record(ctx.world, "quotes", symbol) or {}
    state, as_of = market_state(ctx, ticker, ctx.now_ns)
    return ctx.respond(
        {
            "symbol": symbol,
            "price": quote.get("price"),
            "currency": quote.get("currency", ticker["currency"]),
            "market_state": state,
            "as_of": iso_us_z(as_of),
        }
    )


def market_state(ctx: RequestContext, ticker: JsonObject, now_ns: int) -> tuple[str, int]:
    """``open``/``closed`` and the quote's ``as_of`` instant at ``now_ns`` (exchange sessions)."""
    calendar = ctx.calendar(str(ticker["exchange"]))
    if calendar.is_open(now_ns):
        return "open", now_ns - QUOTE_LAG_NS
    return "closed", calendar.last_close(now_ns) or now_ns


def quote_currency_invariant(world: SessionWorld) -> list[str]:
    """Every quote is priced in its ticker's currency."""
    problems = []
    for quote in _records(world, "quotes"):
        ticker = _record(world, "tickers", quote["symbol"])
        if ticker is not None and ticker["currency"] != quote["currency"]:
            problems.append(f"{quote['symbol']}: {quote['currency']} != {ticker['currency']}")
    return problems


# -- time --------------------------------------------------------------------------------------


async def server_time(ctx: RequestContext) -> Response:
    """Now: 7 fraction digits, the last always 0, and ``unix_ms`` (000200-000214)."""
    ns = ctx.now_ns
    moment = datetime.fromtimestamp(ns // NS, tz=UTC)
    micros = (ns % NS) // 1000
    return ctx.respond(
        {
            "utc": f"{moment:%Y-%m-%dT%H:%M:%S}.{micros:06d}0Z",
            "unix_ms": ns // MS,
            "zone": "UTC",
        }
    )


# -- search ------------------------------------------------------------------------------------

SECTOR_SYNONYMS: Final[dict[str, frozenset[str]]] = {
    # Every word observed to match a whole sector at score 0.5 (search, search-2 recordings),
    # in addition to the sector name's own tokens.
    "Technology": frozenset({"tech", "software", "cloud"}),
    "Financials": frozenset({"bank", "banks", "banking", "finance", "fintech", "payments"}),
    "Automotive": frozenset({"auto", "automaker", "car", "cars", "ev", "vehicle", "vehicles"}),
    "Semiconductors": frozenset({"chip", "chips", "semiconductor", "semis"}),
    "Energy": frozenset({"gas", "oil", "petroleum"}),
    "Healthcare": frozenset({"biotech", "drug", "drugs", "medicine", "pharma"}),
    "Communication Services": frozenset({"media", "social", "streaming", "telecom"}),
    "Industrials": frozenset({"aerospace", "aircraft", "machinery"}),
    "Consumer Discretionary": frozenset({"ecommerce", "luxury", "retail"}),
    "Consumer Staples": frozenset({"beverage", "beverages", "food", "grocery"}),
}
_TOKEN: Final = re.compile(r"[a-z0-9]+")
SYMBOL_EXACT: Final = Decimal("1.0")
SYMBOL_PREFIX: Final = Decimal("0.9")
NAME_EXACT: Final = Decimal("0.8")
NAME_PREFIX: Final = Decimal("0.6")
SECTOR: Final = Decimal("0.5")
MAX_RESULTS: Final = 10


def tokens(text: str) -> list[str]:
    """Lower-cased ASCII alphanumeric runs (``"Moët"`` -> ``["mo", "t"]``)."""
    return _TOKEN.findall(text.lower())


def token_score(token: str, ticker: JsonObject) -> Decimal:
    """The best way one query token matches one ticker."""
    root = str(ticker["symbol"]).split(".", 1)[0].lower()
    if token == root:
        return SYMBOL_EXACT
    if len(token) >= 2 and root.startswith(token):  # noqa: PLR2004 - 'nv', 'ms' (search-2)
        return SYMBOL_PREFIX
    name = tokens(str(ticker["name"]))
    if token in name:
        return NAME_EXACT
    if len(token) >= 3 and any(word.startswith(token) for word in name):  # noqa: PLR2004
        return NAME_PREFIX
    sector = str(ticker["sector"])
    if token in tokens(sector) or token in SECTOR_SYNONYMS.get(sector, frozenset()):
        return SECTOR
    return Decimal(0)


def score(query: str, ticker: JsonObject) -> Decimal:
    """Mean token score (repeated tokens count again), rounded half-up to 3 decimals."""
    words = tokens(query)
    if not words:
        return Decimal(0)
    total = sum((token_score(word, ticker) for word in words), Decimal(0))
    return (total / len(words)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


async def search(ctx: RequestContext) -> Response:
    """Top 10 tickers by score, then symbol (000219-000297, search-2, search-3)."""
    query = _last(ctx, "q")
    if query is None:
        return _missing(ctx, "q")
    if not 2 <= len(query) <= 64:  # noqa: PLR2004
        return _invalid(ctx, "q", "must be a string of 2 to 64 characters")
    scored = [(score(query, t), t) for t in _records(ctx.world, "tickers")]
    hits = sorted(
        ((s, t) for s, t in scored if s > 0), key=lambda pair: (-pair[0], str(pair[1]["symbol"]))
    )
    results: list[JsonValue] = [
        {"symbol": t["symbol"], "name": t["name"], "score": float(s)} for s, t in hits[:MAX_RESULTS]
    ]
    return ctx.respond({"query": query, "results": results})


# -- watchlists --------------------------------------------------------------------------------

WATCHLIST_FIRST_ID: Final = 1001
NAME_RULE: Final = "must be a string of 1 to 50 characters"
SYMBOLS_RULE: Final = "must be an array of strings"


def _watchlist_id(ctx: RequestContext) -> int | Response:
    parsed = lax_int(ctx.path_param("id") or "")
    if parsed is None:
        return _invalid(ctx, "id", "must be an integer")
    return parsed


def _json_object(ctx: RequestContext) -> JsonObject | Response:
    """The request body as a JSON object (000461-000464, 000723)."""
    if ctx.json_error:
        return api_error(ctx, 400, "invalid_json", "Request body is not valid JSON.")
    if not isinstance(ctx.json_body, dict):
        return api_error(ctx, 400, "invalid_body", "Request body must be a JSON object.")
    return ctx.json_body


def _name(ctx: RequestContext, body: JsonObject) -> str | Response:
    value = body["name"]
    if not isinstance(value, str) or not 1 <= len(value) <= 50:  # noqa: PLR2004
        return _invalid(ctx, "name", NAME_RULE)
    return value


def _symbols(ctx: RequestContext, body: JsonObject) -> list[str] | Response:
    """Upper-cased, de-duplicated (first wins) known symbols (000445, 000448, 000468, 000469)."""
    value = body["symbols"]
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        return _invalid(ctx, "symbols", SYMBOLS_RULE)
    symbols: list[str] = []
    for raw in value:
        assert isinstance(raw, str)  # noqa: S101 - checked above (narrowing for mypy)
        if _ticker(ctx, raw) is None:
            return api_error(
                ctx, 400, "unknown_symbol", f"Unknown symbol '{raw}'.", param="symbols"
            )
        if raw.upper() not in symbols:
            symbols.append(raw.upper())
    return symbols


def _watchlist_fields(
    ctx: RequestContext, body: JsonObject, *, required: tuple[str, ...]
) -> JsonObject | Response:
    """Validated ``name``/``symbols`` present in ``body`` (``required`` ones must be)."""
    fields: JsonObject = {}
    for field, check in (("name", _name), ("symbols", _symbols)):
        if field not in body:
            if field in required:
                return _missing(ctx, field)
            continue
        value = check(ctx, body)
        if isinstance(value, Response):
            return value
        fields[field] = cast("JsonValue", value)
    return fields


def _not_found_watchlist(ctx: RequestContext, key: int) -> Response:
    return api_error(ctx, 404, "watchlist_not_found", f"No watchlist with id {key}.")


async def list_watchlists(ctx: RequestContext) -> Response:
    """Every watchlist, sorted by id (000442, 000447, 000500)."""
    found = sorted(_records(ctx.world, "watchlists"), key=lambda w: int(str(w["id"])))
    return ctx.respond(list(found))


async def create_watchlist(ctx: RequestContext) -> Response:
    """201 + Location; ids from 1001, never reused (000443-000469, 000506)."""
    if (denied := _read_only(ctx)) is not None:
        return denied
    body = _json_object(ctx)
    if isinstance(body, Response):
        return body
    fields = _watchlist_fields(ctx, body, required=("name",))
    if isinstance(fields, Response):
        return fields
    key = ctx.runtime.next_counter(ctx.session, "watchlist_id", WATCHLIST_FIRST_ID, 1)
    while ctx.world.get("watchlists", key) is not None:
        key = ctx.runtime.next_counter(ctx.session, "watchlist_id", WATCHLIST_FIRST_ID, 1)
    stamp = iso_ms_z(ctx.now_ns)
    record: JsonObject = {
        "id": key,
        "name": fields["name"],
        "symbols": fields.get("symbols", []),
        "created_at": stamp,
        "updated_at": stamp,
    }
    ctx.world.insert("watchlists", record)
    return ctx.respond(record, status=201, headers={"location": f"/v1/watchlists/{key}"})


async def get_watchlist(ctx: RequestContext) -> Response:
    """One watchlist (000444, 000470-000476)."""
    key = _watchlist_id(ctx)
    if isinstance(key, Response):
        return key
    record = _record(ctx.world, "watchlists", key)
    return ctx.respond(record) if record is not None else _not_found_watchlist(ctx, key)


async def _change_watchlist(ctx: RequestContext, *, required: tuple[str, ...]) -> Response:
    """PUT/PATCH: read-only, id, body, then existence (000480-000498, 000719-000721)."""
    if (denied := _read_only(ctx)) is not None:
        return denied
    key = _watchlist_id(ctx)
    if isinstance(key, Response):
        return key
    body = _json_object(ctx)
    if isinstance(body, Response):
        return body
    fields = _watchlist_fields(ctx, body, required=required)
    if isinstance(fields, Response):
        return fields
    if _record(ctx.world, "watchlists", key) is None:
        return _not_found_watchlist(ctx, key)
    # updated_at moves on every successful change, even an empty PATCH (000493, 000498).
    stored = ctx.world.update("watchlists", key, {**fields, "updated_at": iso_ms_z(ctx.now_ns)})
    assert stored is not None  # noqa: S101 - existence checked above
    return ctx.respond(stored.record)


async def replace_watchlist(ctx: RequestContext) -> Response:
    """PUT: both fields required."""
    return await _change_watchlist(ctx, required=("name", "symbols"))


async def update_watchlist(ctx: RequestContext) -> Response:
    """PATCH: only the fields present (``symbols`` replaced, not appended)."""
    return await _change_watchlist(ctx, required=())


async def delete_watchlist(ctx: RequestContext) -> Response:
    """204; deleting again is a 404 (000502-000505)."""
    if (denied := _read_only(ctx)) is not None:
        return denied
    key = _watchlist_id(ctx)
    if isinstance(key, Response):
        return key
    if not ctx.world.delete("watchlists", key):
        return _not_found_watchlist(ctx, key)
    return ctx.empty(204)


# -- uploads -----------------------------------------------------------------------------------

QUEUED_NS: Final = 2 * NS
PROCESSING_NS: Final = 6 * NS
MAX_UPLOAD_BYTES: Final = 1_048_576
OUTCOME_FIELD: Final = "_outcome"
"""Internal: the upload's terminal state, decided from its bytes at upload time (not served)."""
_PUBLIC_ORDER: Final = (
    "id",
    "status",
    "filename",
    "size",
    "label",
    "created_at",
    "progress",
    "completed_at",
    "error",
)


def upload_view(record: JsonObject, now_ns: int) -> JsonObject:
    """The upload as served at ``now_ns``: state derived from the time since creation.

    queued for 2 s, processing for 6 s with ``progress`` = floor(elapsed / 6 s x 100), then the
    terminal outcome with ``completed_at`` = created_at + 8 s (000516-000668).
    """
    created_ns = parse_isoformat_ns(str(record["created_at"]))
    elapsed = now_ns - created_ns
    view: JsonObject = {k: record[k] for k in _PUBLIC_ORDER[:6]}
    if elapsed < QUEUED_NS:
        view.update(status="queued", progress=0)
    elif elapsed < QUEUED_NS + PROCESSING_NS:
        view.update(status="processing", progress=(elapsed - QUEUED_NS) * 100 // PROCESSING_NS)
    else:
        outcome = str(record.get(OUTCOME_FIELD) or "done")
        view.update(status=outcome, progress=100)
        view["completed_at"] = python_isoformat(created_ns + QUEUED_NS + PROCESSING_NS)
        if outcome == "failed":
            view["error"] = "corrupt_file"
    return {k: view[k] for k in _PUBLIC_ORDER if k in view}


def _upload(ctx: RequestContext) -> tuple[str, JsonObject | None]:
    key = ctx.path_param("id") or ""
    return key, _record(ctx.world, "uploads", key)


def _upload_not_found(ctx: RequestContext, key: str) -> Response:
    return api_error(ctx, 404, "upload_not_found", f"No upload with id '{key}'.")


def ascii_filename(name: str) -> str:
    """Content-Disposition spelling: non-ASCII characters become ``_`` (000689)."""
    return "".join(c if c.isascii() else "_" for c in name)


async def create_upload(ctx: RequestContext) -> Response:
    """202 queued; processing outcome decided by the bytes (000516-000675, 000694-000698)."""
    if (denied := _read_only(ctx)) is not None:
        return denied
    request = ctx.request
    assert isinstance(request, Request)  # noqa: S101 - HTTP endpoint
    form = await request.form()
    parts = form.getlist("file")
    if not parts:
        return _missing(ctx, "file")
    part = parts[-1]  # the last 'file' part wins (000695)
    if not isinstance(part, UploadFile):
        return _invalid(ctx, "file", "must be a file")
    content = await part.read()
    if len(content) > MAX_UPLOAD_BYTES:
        return api_error(
            ctx,
            413,
            "payload_too_large",
            f"File exceeds the 1 MiB ({MAX_UPLOAD_BYTES} bytes) limit.",
        )
    labels = [v for v in form.getlist("label") if isinstance(v, str)]
    content_type = part.content_type or "application/octet-stream"
    key = _uuid4(ctx)
    record: JsonObject = {
        "id": key,
        "status": "queued",
        "filename": part.filename or "",
        "size": len(content),
        "label": labels[-1] if labels and labels[-1] else None,
        "created_at": python_isoformat(ctx.now_ns),
        "progress": 0,
        # ASCII 'CORRUPT' anywhere in the bytes fails the job; case-sensitive (000536, 000620).
        OUTCOME_FIELD: "failed" if b"CORRUPT" in content else "done",
    }
    ctx.world.insert("uploads", record)
    ctx.runtime.pending_blob(ctx, "file", content, content_type, part.filename)
    ctx.runtime.commit_blob(ctx, "uploads", key)
    return ctx.respond(upload_view(record, ctx.now_ns), status=202)


async def list_uploads(ctx: RequestContext) -> Response:
    """Every upload, newest first, each in its current state (000670, 000693)."""
    found = _records(ctx.world, "uploads")
    found.sort(key=lambda u: parse_isoformat_ns(str(u["created_at"])), reverse=True)
    return ctx.respond([upload_view(u, ctx.now_ns) for u in found])


async def get_upload(ctx: RequestContext) -> Response:
    """Current state; ids are matched exactly (000676-000678, 000692)."""
    key, record = _upload(ctx)
    if record is None:
        return _upload_not_found(ctx, key)
    return ctx.respond(upload_view(record, ctx.now_ns))


async def delete_upload(ctx: RequestContext) -> Response:
    """204; the upload and its bytes are gone (000681-000686)."""
    if (denied := _read_only(ctx)) is not None:
        return denied
    key, record = _upload(ctx)
    if record is None:
        return _upload_not_found(ctx, key)
    ctx.world.delete("uploads", key)
    ctx.runtime.drop_blob(ctx, "uploads", key)
    return ctx.empty(204)


async def download_upload(ctx: RequestContext) -> Response:
    """The original bytes once done, else 409 not_ready (000518, 000533, 000552, 000689-000691)."""
    key, record = _upload(ctx)
    if record is None:
        return _upload_not_found(ctx, key)
    status = str(upload_view(record, ctx.now_ns)["status"])
    if status != "done":
        return api_error(
            ctx,
            409,
            "not_ready",
            f"Upload '{key}' is {status}; content is available only when status is done.",
        )
    blob = ctx.runtime.blob(ctx, "uploads", key)
    content = blob.content if blob is not None else b""
    content_type = (blob.content_type if blob else None) or "application/octet-stream"
    disposition = f'attachment; filename="{ascii_filename(str(record["filename"]))}"'
    return ctx.respond_bytes(
        content,
        status=200,
        content_type=content_type,
        headers={"content-disposition": disposition},
    )


# -- trade stream ------------------------------------------------------------------------------

TRADE_SPACING_S: Final = 0.2
"""Events are 200 ms apart (documentation; ~202 ms observed in every session)."""


@cache
def trade_walks(data_dir: Path) -> dict[str, list[tuple[str, int]]]:
    """Recorded walk of every symbol (``scripts/build_trade_walks.py``)."""
    path = data_dir / "trade_walks.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {symbol: [(str(p), int(s)) for p, s in walk] for symbol, walk in raw.items()}


async def stream_trades(ctx: RequestContext) -> Response:
    """SSE: ``count`` trade events (id 1..count) from the symbol's deterministic walk."""
    symbol_raw = _last(ctx, "symbol")
    if symbol_raw is None:
        return _missing(ctx, "symbol")
    count = 10
    count_raw = _last(ctx, "count")
    if count_raw is not None:
        parsed = lax_int(count_raw)
        if parsed is None or not 1 <= parsed <= 50:  # noqa: PLR2004
            return _invalid(ctx, "count", "must be an integer between 1 and 50")
        count = parsed
    ticker = _ticker(ctx, symbol_raw)
    if ticker is None:
        return api_error(
            ctx, 400, "unknown_symbol", f"Unknown symbol '{symbol_raw}'.", param="symbol"
        )
    symbol = str(ticker["symbol"])
    walk = trade_walks(ctx.runtime.settings.data_dir).get(symbol, [])
    if len(walk) < count:  # no recording this long: hold the quote price (never observed)
        quote = _record(ctx.world, "quotes", symbol) or {}
        walk = [*walk, *[(str(quote.get("price")), 100)] * (count - len(walk))]
    clock = ctx.runtime.clocks.for_session(ctx.session)

    async def events() -> AsyncIterator[bytes]:
        for index, (price, size) in enumerate(walk[:count], start=1):
            if index > 1:
                await ctx.runtime.pause(TRADE_SPACING_S)
            data = {
                "symbol": symbol,
                "price": price,
                "size": size,
                "time": iso_ms_z(clock.now_ns()),
            }
            payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            yield f"id: {index}\nevent: trade\ndata: {payload}\n\n".encode()

    headers = {"cache-control": "no-cache", **ctx.extra_headers}
    return StreamingResponse(
        events(), status_code=200, media_type="text/event-stream; charset=utf-8", headers=headers
    )


HANDLERS.update(
    {
        "GET /v1/exchanges/{code}": get_exchange,
        "GET /v1/tickers": list_tickers,
        "GET /v1/tickers/{symbol}": get_ticker,
        "GET /v1/tickers/{symbol}/quote": get_quote,
        "GET /v1/search": search,
        "GET /v1/time": server_time,
        "GET /v1/watchlists": list_watchlists,
        "POST /v1/watchlists": create_watchlist,
        "GET /v1/watchlists/{id}": get_watchlist,
        "PUT /v1/watchlists/{id}": replace_watchlist,
        "PATCH /v1/watchlists/{id}": update_watchlist,
        "DELETE /v1/watchlists/{id}": delete_watchlist,
        "GET /v1/uploads": list_uploads,
        "POST /v1/uploads": create_upload,
        "GET /v1/uploads/{id}": get_upload,
        "DELETE /v1/uploads/{id}": delete_upload,
        "GET /v1/uploads/{id}/content": download_upload,
        "GET /v1/stream/trades": stream_trades,
    }
)

# -- WebSocket quote feed ----------------------------------------------------------------------

WS_ROUNDS: Final = 5
"""A subscription streams 5 rounds of quotes, then ``done`` and close 1000 (000811, 000813)."""
WS_SPACING_S: Final = 0.25
"""Rounds are ~250 ms apart; the first round follows the ``subscribed`` ack at once."""
WS_AUTH_CLOSE: Final = 4401
WS_AUTH_REASONS: Final = {
    "missing": "Missing api_key query parameter.",
    "invalid": "Invalid API key.",
    "forbidden": "This API key has been revoked.",
}
"""Close reasons on bad credentials (000827-000829, 000834). Only ``?api_key`` counts (000832)."""
WS_BAD_SYMBOLS: Final = "symbols must be a non-empty array of strings."
WS_BINARY: Final = "Binary messages are not supported."
"""Any binary frame earns this error and the socket stays open (independent review, 2026-10-11)."""


def ws_auth_failure(ctx: RequestContext, api_key: str | None) -> str | None:
    """The close reason for this ``api_key`` query value, or ``None`` when it is accepted."""
    if not api_key:  # absent or empty (000831)
        return WS_AUTH_REASONS["missing"]
    outcome = ctx.runtime.auth.authenticate(
        headers={"x-api-key": api_key}, query={}, cookies={}, now_ns=ctx.now_ns, required=True
    )
    if outcome.failure is None:
        return None
    return WS_AUTH_REASONS.get(outcome.failure, WS_AUTH_REASONS["invalid"])


def ws_subscription(ctx: RequestContext, text: str) -> list[JsonObject] | str:
    """The tickers a client message subscribes to, or the error message it earns.

    Checked in the real order: JSON (000824), an object (000825, 000843), ``action`` exactly
    ``subscribe`` (shown JSON-encoded: 000822, 000823, 000838, 000842), ``symbols`` a non-empty
    array of strings (000819-000821, 000837), then every symbol known, case-insensitively; the
    first unknown one is reported as sent, in single quotes (000817, 000836, 000840, 000841).
    Duplicates collapse.
    """
    try:
        message = json.loads(text)
    except ValueError:
        return "Invalid JSON."
    if not isinstance(message, dict):
        return "Messages must be JSON objects."
    action = message.get("action")
    if action != "subscribe":
        return f"Unknown action: {json.dumps(action, ensure_ascii=False, separators=(',', ':'))}."
    symbols = message.get("symbols")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(s, str) for s in symbols):
        return WS_BAD_SYMBOLS
    tickers: dict[str, JsonObject] = {}
    for raw in cast("list[str]", symbols):
        ticker = _ticker(ctx, raw)
        if ticker is None:
            return f"Unknown symbol '{raw}'."
        tickers.setdefault(str(ticker["symbol"]), ticker)
    return list(tickers.values())


def ws_quote(ctx: RequestContext, ticker: JsonObject, now_ns: int) -> JsonObject:
    """One ``quote`` message: the REST quote's price and ``as_of`` at ``now_ns`` (000813)."""
    symbol = str(ticker["symbol"])
    quote = _record(ctx.world, "quotes", symbol) or {}
    _, as_of = market_state(ctx, ticker, now_ns)
    return {
        "type": "quote",
        "symbol": symbol,
        "price": quote.get("price"),
        "as_of": iso_us_z(as_of),
    }


async def _send(websocket: WebSocket, message: JsonObject) -> None:
    await websocket.send_text(json.dumps(message, separators=(",", ":"), ensure_ascii=False))


async def ws_quotes(websocket: WebSocket, ctx: RequestContext) -> None:
    """``WS /v1/ws/quotes``: one subscription per connection, answered in message order.

    Messages are handled one at a time; an error leaves the socket open (000835), a valid
    subscription streams and then closes, so messages sent meanwhile are never answered (000826).
    An idle socket stays open (000833: 20 s). Not counted against the rate limit (000812/000814).
    """
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415 - runtime-only import

    await websocket.accept()
    reason = ws_auth_failure(ctx, websocket.query_params.get("api_key"))
    if reason is not None:
        await websocket.close(code=WS_AUTH_CLOSE, reason=reason)
        return
    clock = ctx.runtime.clocks.for_session(ctx.session)
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:  # a binary frame
                await _send(websocket, {"type": "error", "message": WS_BINARY})
                continue
            subscription = ws_subscription(ctx, text)
            if isinstance(subscription, str):
                await _send(websocket, {"type": "error", "message": subscription})
                continue
            symbols: list[JsonValue] = [str(t["symbol"]) for t in subscription]
            await _send(websocket, {"type": "subscribed", "symbols": symbols})
            for round_index in range(WS_ROUNDS):
                if round_index:
                    await ctx.runtime.pause(WS_SPACING_S)
                now_ns = clock.now_ns()
                for ticker in subscription:
                    await _send(websocket, ws_quote(ctx, ticker, now_ns))
            await _send(websocket, {"type": "done"})
            await websocket.close(code=1000)
            return
    except WebSocketDisconnect:
        return


WEBSOCKET_HANDLERS["GET /v1/ws/quotes"] = ws_quotes


# -- routes no endpoint matches ----------------------------------------------------------------

fallback_router = APIRouter()
"""Catch-all for unknown routes and wrong methods (registered after the generated routes).

Under ``/v1/`` the real server authenticates and rate-limits first, so a missing key is a 401 even
on an unknown route (000013) and 404/405 answers carry the rate-limit headers (000010-000014).
405s list the path's methods in ``Allow`` (000014: ``GET``; 000514: ``GET, POST``).
"""

_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_WRITE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
RATE_LIMITED_PROXY: Final = "GET /v1/exchanges"
"""Any /v1 endpoint: unknown /v1 routes count against the same per-key limit."""


def allowed_methods(templates: Sequence[tuple[str, str]], path: str) -> list[str]:
    """Methods of every endpoint whose path template matches ``path`` (model order)."""
    methods: list[str] = []
    for method, template in templates:
        pattern = re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template))
        if re.fullmatch(pattern, path) and method not in methods:
            methods.append(method)
    return methods


@fallback_router.api_route("/{rest:path}", methods=_METHODS, include_in_schema=False)
async def not_routed(request: Request) -> Response:
    """404 ``not_found`` / 405 ``method_not_allowed``, authenticated under ``/v1/``."""
    runtime = runtime_of(request)
    ctx = runtime.new_context(request, None)
    path = request.url.path
    if path == "/v1" or path.startswith("/v1/"):
        outcome = runtime.auth.authenticate(
            headers=dict(request.headers),
            query=dict(request.query_params),
            cookies=dict(request.cookies),
            now_ns=ctx.now_ns,
            required=True,
        )
        ctx.identity = outcome.identity
        if outcome.failure is not None:
            return runtime._auth_error(ctx, outcome.failure)  # noqa: SLF001
        ctx.endpoint = runtime.model.endpoint(RATE_LIMITED_PROXY)  # same per-key limit
        limited = runtime._rate_limit(ctx)  # noqa: SLF001
        ctx.endpoint = None
        if limited is not None:
            return limited
        if request.method in _WRITE_METHODS and (read_only := _read_only(ctx)) is not None:
            return read_only  # the read-only check runs before routing (independent review)
    templates = [(e.method, e.path) for e in runtime.model.endpoints if e.role != "websocket"]
    methods = allowed_methods(templates, path)
    if methods:
        return ctx.error(
            "method_not_allowed",
            template=runtime.model.routing.method_not_allowed_template,
            headers={"allow": ", ".join(methods)},
        )
    return ctx.error("unknown_route", template=runtime.model.routing.unknown_route_template)
