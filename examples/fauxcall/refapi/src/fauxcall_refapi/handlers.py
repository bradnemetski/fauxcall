"""Custom behavior of the FauxCall Reference Market API fake.

The real API validates in a precise order with its own messages, matches codes and symbols
case-insensitively and computes several answers (quotes from exchange calendars, search scores,
upload states from elapsed time), so most ``/v1`` endpoints are implemented here on top of the
world (``ctx.world``), the model's error templates (``ctx.error(..., template=...)``) and its
calendars. Every rule below comes from a recording; the exchange ids in comments are evidence.

The generic engine still does what surrounds every call: authentication (``X-API-Key``,
read-only and revoked keys), the per-key rate limit and its headers, routing errors, latency.
Validation helpers raise :class:`EarlyResponse` with the real API's error, which the runtime
answers as is.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final, cast

from fauxcall.runtime import EarlyResponse
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from fastapi import Response, WebSocket
    from fauxcall.model.errors import ErrorCondition
    from fauxcall.model.world import WorldRecord
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue
    from starlette.requests import Request

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

METHOD_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP method -> custom handler of its params (``None``: the recordings answer)."""

NS: Final = 1_000_000_000
QUOTE_LAG_NS: Final = 1_500_000_000  # as_of = now - 1.5 s while open (000047-000058)
MAX_UPLOAD: Final = 1_048_576  # 1 MiB accepted (000437), one byte more refused (000438)
QUEUED_S: Final = 2.0  # upload states (000370-000384, 000405-000414)
PROCESSING_S: Final = 6.0
FIRST_TRADE_S: Final = 0.05  # SSE: first event ~55 ms after the request (000468)
TRADE_SPACING_S: Final = 0.2  # then 200 ms apart
WS_ROUNDS: Final = 5  # WebSocket: 5 rounds of quotes, ~252 ms apart (000485, 000733)
WS_SPACING_S: Final = 0.25
TRADES: Final = "GET /v1/stream/trades"
FORM_TYPES: Final = ("multipart/form-data", "application/x-www-form-urlencoded")  # 000807


# -- helpers --------------------------------------------------------------------------------------


def _fail(  # noqa: PLR0913 - mirrors ctx.error's keywords
    ctx: RequestContext,
    condition: ErrorCondition,
    template: str,
    *,
    param: str | None = None,
    value: JsonValue = None,
    values: dict[str, JsonValue] | None = None,
) -> EarlyResponse:
    """The real API's error for this request, to ``raise``."""
    return EarlyResponse(
        ctx.error(condition, template=template, param=param, value=value, values=values)
    )


def _last(ctx: RequestContext, name: str) -> str | None:
    """A query parameter's last value: the real API reads repeated parameters that way (000124)."""
    values = ctx.request.query_params.getlist(name)
    return values[-1] if values else None


_INTEGER: Final = TypeAdapter(int)


def _int(text: str) -> int | None:
    """An integer as the real API's validation (Pydantic, lax) parses it.

    '05', '+5', ' 5' and '7.0' are integers (000695-000697; '7.0' -> id 7, seen in parity);
    '1.5', '1e1' and '' are not (000137, 000698, 000128).
    """
    try:
        return _INTEGER.validate_python(text)
    except ValidationError:
        return None


def _bounded(ctx: RequestContext, name: str, default: int, upper: int, template: str) -> int:
    """An optional integer query parameter between 1 and ``upper`` (else the template's 400)."""
    raw = _last(ctx, name)
    if raw is None:
        return default
    parsed = _int(raw)
    if parsed is None or not 1 <= parsed <= upper:
        raise _fail(ctx, "invalid_param", template, param=name, value=raw)
    return parsed


def _iso_z(ns: int, digits: int) -> str:
    """UTC instant as ``YYYY-MM-DDTHH:MM:SS.fff…Z`` (fraction truncated to ``digits``)."""
    seconds, rest = divmod(ns, NS)
    stamp = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}.{rest:09d}"[: len(stamp) + 1 + digits] + "Z"


def _isoformat(us: int) -> str:
    """Python ``datetime.isoformat()`` of a UTC instant in microseconds (uploads' timestamps)."""
    seconds, micros = divmod(us, 1_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=micros).isoformat()


def _parse_isoformat_us(text: str) -> int:
    moment = datetime.fromisoformat(text)
    whole = int(moment.replace(microsecond=0).timestamp())
    return whole * 1_000_000 + moment.microsecond


def _live_ns(ctx: RequestContext) -> int:
    """The fake clock now (not the request's start), for events sent later in a stream."""
    return ctx.runtime.clocks.for_session(ctx.session).now_ns()


def _plain(record: WorldRecord) -> JsonObject:
    value = record.record
    assert isinstance(value, dict)  # noqa: S101 - every collection holds objects
    return dict(value)


def _find(ctx: RequestContext, collection: str, key: JsonValue) -> JsonObject | None:
    record = ctx.world.get(collection, key)
    return None if record is None else _plain(record)


def _require(ctx: RequestContext, collection: str, key: str) -> JsonObject:
    """A record that must exist (the relations keep quotes and tickers consistent)."""
    record = _find(ctx, collection, key)
    assert record is not None  # noqa: S101 - quote_ticker / ticker_exchange relations
    return record


def _dumps(value: JsonValue) -> str:
    """Compact JSON, as the real API writes it."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# -- exchanges and tickers ------------------------------------------------------------------------


async def get_exchange(ctx: RequestContext) -> Response:
    """One exchange; the code is case-insensitive and echoed upper-cased in the 404 (000706)."""
    code = (ctx.path_param("code") or "").upper()
    record = ctx.world.get("exchanges", code)
    if record is None:
        return ctx.error("not_found", template="exchange_not_found", value=code)
    return ctx.respond(ctx.render_record(record))


async def get_ticker(ctx: RequestContext) -> Response:
    """One ticker; the symbol is case-insensitive and echoed upper-cased in the 404 (000707)."""
    symbol = (ctx.path_param("symbol") or "").upper()
    record = ctx.world.get("tickers", symbol)
    if record is None:
        return ctx.error("not_found", template="ticker_not_found", value=symbol)
    return ctx.respond(ctx.render_record(record))


def _encode_cursor(symbol: str) -> str:
    raw = json.dumps({"after": symbol}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(text: str) -> str | None:
    """The cursor's ``after`` symbol; padding and JSON spacing are tolerated (000685, 000688)."""
    try:
        decoded = base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode("ascii"))
        value = json.loads(decoded)
    except (ValueError, binascii.Error, UnicodeError):
        return None
    after = value.get("after") if isinstance(value, dict) else None
    return after if isinstance(after, str) else None


def _cursor_symbol(ctx: RequestContext) -> str | None:
    """The symbol to continue after: it must name a ticker, in any case (000139, 000684)."""
    raw = _last(ctx, "cursor") or None  # an empty cursor is ignored (000127)
    if raw is None:
        return None
    decoded = _decode_cursor(raw)
    after = decoded.upper() if decoded is not None else None
    if after is None or ctx.world.get("tickers", after) is None:
        raise _fail(ctx, "invalid_param", "invalid_cursor", param="cursor")
    return after


async def list_tickers(ctx: RequestContext) -> Response:
    """Tickers by symbol; checks limit > exchange > sector > cursor (000142, 000677-000680)."""
    limit = _bounded(ctx, "limit", 20, 100, "invalid_limit")
    exchange = _last(ctx, "exchange") or None  # empty filters are ignored (000125, 000126)
    if exchange is not None and ctx.world.get("exchanges", exchange.upper()) is None:
        raise _fail(ctx, "invalid_param", "unknown_exchange", param="exchange", value=exchange)
    tickers = sorted(map(_plain, ctx.world.list("tickers")), key=lambda t: str(t["symbol"]))
    sector = _last(ctx, "sector") or None
    if sector is not None and sector.lower() not in {str(t["sector"]).lower() for t in tickers}:
        raise _fail(ctx, "invalid_param", "unknown_sector", param="sector", value=sector)
    after = _cursor_symbol(ctx)
    matches = [
        ticker
        for ticker in tickers
        if (exchange is None or ticker["exchange"] == exchange.upper())
        and (sector is None or str(ticker["sector"]).lower() == sector.lower())
    ]
    rest = [ticker for ticker in matches if after is None or str(ticker["symbol"]) > after]
    page = rest[:limit]
    body: JsonObject = {
        "data": list(page),
        "next_cursor": _encode_cursor(str(page[-1]["symbol"])) if len(rest) > limit else None,
        "total": len(matches),
    }
    return ctx.respond(body)


# -- quotes ---------------------------------------------------------------------------------------


def quote_state(ctx: RequestContext, exchange: str, now_ns: int) -> tuple[str, str]:
    """``(market_state, as_of)``: now - 1.5 s while the exchange is open, else its last close."""
    calendar = ctx.calendar(exchange)
    if calendar.is_open(now_ns):
        return "open", _iso_z(now_ns - QUOTE_LAG_NS, 6)
    last_close = calendar.last_close(now_ns)
    return "closed", _iso_z(last_close if last_close is not None else now_ns, 6)


async def get_quote(ctx: RequestContext) -> Response:
    """The ticker's static price; market_state/as_of follow its exchange's sessions."""
    symbol = (ctx.path_param("symbol") or "").upper()
    ticker, quote = _find(ctx, "tickers", symbol), _find(ctx, "quotes", symbol)
    if ticker is None or quote is None:
        return ctx.error("not_found", template="ticker_not_found", value=symbol)
    state, as_of = quote_state(ctx, str(ticker["exchange"]), ctx.now_ns)
    body: JsonObject = {
        "symbol": symbol,
        "price": quote["price"],
        "currency": quote["currency"],
        "market_state": state,
        "as_of": as_of,
    }
    return ctx.respond(body)


# -- search ---------------------------------------------------------------------------------------

_TOKEN: Final = re.compile(r"[a-z0-9]+")

SYNONYMS: Final[dict[str, str]] = {
    # Every entry was observed to score 0.5 on the whole sector (06-search, 15-search-2);
    # words recorded with no sector match (e.g. 'financial', 'hardware', 'trucks') are absent.
    "tech": "Technology",
    "software": "Technology",
    "cloud": "Technology",
    "chip": "Semiconductors",
    "chips": "Semiconductors",
    "semis": "Semiconductors",
    "chipmaker": "Semiconductors",
    "semiconductor": "Semiconductors",
    "car": "Automotive",
    "cars": "Automotive",
    "auto": "Automotive",
    "autos": "Automotive",
    "ev": "Automotive",
    "vehicle": "Automotive",
    "vehicles": "Automotive",
    "bank": "Financials",
    "banks": "Financials",
    "banking": "Financials",
    "finance": "Financials",
    "payments": "Financials",
    "fintech": "Financials",
    "pharma": "Healthcare",
    "biotech": "Healthcare",
    "drugs": "Healthcare",
    "medicine": "Healthcare",
    "media": "Communication Services",
    "telecom": "Communication Services",
    "streaming": "Communication Services",
    "social": "Communication Services",
    "oil": "Energy",
    "gas": "Energy",
    "petroleum": "Energy",
    "retail": "Consumer Discretionary",
    "luxury": "Consumer Discretionary",
    "ecommerce": "Consumer Discretionary",
    "food": "Consumer Staples",
    "beverages": "Consumer Staples",
    "aerospace": "Industrials",
    "machinery": "Industrials",
}
SYMBOL_EXACT: Final = Decimal("1.0")  # 'aapl', '0700' (base of 0700.HK), 'v ' (000162, 000202)
SYMBOL_PREFIX: Final = Decimal("0.9")  # >= 2 chars: 'ai' -> AIR.PA, 'ts' -> TSLA/TSM (000221)
NAME_TOKEN: Final = Decimal("0.8")  # 'apple', 'corp', 't' of 'Moët' (000188, 000203)
NAME_PREFIX: Final = Decimal("0.6")  # >= 3 chars: 'app', 'micro', 'motor' -> Motors
SECTOR: Final = Decimal("0.5")  # a token of the sector's name, or a synonym
MIN_SYMBOL_PREFIX: Final = 2
MIN_NAME_PREFIX: Final = 3
MAX_RESULTS: Final = 10


def tokens(text: str) -> list[str]:
    """Lower-case ASCII alphanumeric runs ('Moët' -> ['mo', 't'], 'p.l.c' -> ['p', 'l', 'c'])."""
    return _TOKEN.findall(text.lower())


def _token_score(token: str, ticker: JsonObject) -> Decimal:
    base = str(ticker["symbol"]).split(".", maxsplit=1)[0].lower()
    names = tokens(str(ticker["name"]))
    sector = str(ticker["sector"])
    if token == base:
        return SYMBOL_EXACT
    if len(token) >= MIN_SYMBOL_PREFIX and base.startswith(token):
        return SYMBOL_PREFIX
    if token in names:
        return NAME_TOKEN
    if len(token) >= MIN_NAME_PREFIX and any(name.startswith(token) for name in names):
        return NAME_PREFIX
    if token in tokens(sector) or SYNONYMS.get(token) == sector:
        return SECTOR
    return Decimal(0)


def score(query: str, ticker: JsonObject) -> Decimal:
    """Mean of the query tokens' best matches, rounded half-up to 3 decimals."""
    words = tokens(query)
    if not words:
        return Decimal(0)
    total = sum((_token_score(word, ticker) for word in words), Decimal(0))
    return (total / len(words)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


async def search(ctx: RequestContext) -> Response:
    """Top 10 tickers by score (desc) then symbol; q must be 2-64 characters (000223-000227)."""
    query = _last(ctx, "q")
    if query is None:
        raise _fail(ctx, "missing_param", "missing_parameter", param="q")
    if not 2 <= len(query) <= 64:  # noqa: PLR2004 - documented bounds
        raise _fail(ctx, "invalid_param", "invalid_q", param="q", value=query)
    hits = []
    for ticker in map(_plain, ctx.world.list("tickers")):
        value = score(query, ticker)
        if value > 0:
            hits.append((value, str(ticker["symbol"]), str(ticker["name"])))
    hits.sort(key=lambda hit: (-hit[0], hit[1]))
    results: list[JsonValue] = [
        {"symbol": symbol, "name": name, "score": float(value)}
        for value, symbol, name in hits[:MAX_RESULTS]
    ]
    return ctx.respond({"query": query, "results": results})


# -- server time ----------------------------------------------------------------------------------


async def server_time(ctx: RequestContext) -> Response:
    """The clock with microsecond precision written as 7 fraction digits (.NET style, 000081)."""
    micros = ctx.now_ns // 1000
    stamp = datetime.fromtimestamp(micros // 1_000_000, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    utc = f"{stamp}.{micros % 1_000_000:06d}0Z"
    return ctx.respond({"utc": utc, "unix_ms": ctx.now_ns // 1_000_000, "zone": "UTC"})


# -- watchlists -----------------------------------------------------------------------------------


def _watchlist_id(ctx: RequestContext) -> int:
    """The path id ('007' -> 7, '+5' -> 5, '7.0' -> 7; 'abc', '1.5' refused, 000262, 000265)."""
    raw = ctx.path_param("id") or ""
    parsed = _int(raw)
    if parsed is None:
        raise _fail(ctx, "invalid_param", "invalid_id", param="id", value=raw)
    return parsed


def _json_object(ctx: RequestContext) -> JsonObject:
    """The body as a JSON object.

    Any JSON media type (application/json, application/vnd.api+json, any case: 000800, 000801);
    text/plain, forms, no type or an empty body are invalid_body (000256, 000720, 000797, 000799);
    unparsable JSON is invalid_json (000255).
    """
    media = (ctx.header("content-type") or "").split(";")[0].strip().lower()
    if "json" not in media or not ctx.body_bytes:
        raise _fail(ctx, "invalid_body", "invalid_body")
    try:
        value = json.loads(ctx.body_bytes)
    except (ValueError, UnicodeDecodeError):
        raise _fail(ctx, "invalid_body", "invalid_json") from None
    if not isinstance(value, dict):
        raise _fail(ctx, "invalid_body", "invalid_body")
    return value


def _name(ctx: RequestContext, body: JsonObject) -> str:
    """The required, 1-50 character name (missing: 000246; invalid: 000248-000251)."""
    if "name" not in body:
        raise _fail(ctx, "missing_param", "missing_parameter", param="name")
    value = body["name"]
    if not isinstance(value, str) or not 1 <= len(value) <= 50:  # noqa: PLR2004
        raise _fail(ctx, "invalid_param", "invalid_name", param="name", value=value)
    return value


def _symbol_list(ctx: RequestContext, value: JsonValue) -> list[str]:
    """An array of strings (null, a string or other items are refused, 000243, 000719)."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(ctx, "invalid_param", "invalid_symbols", param="symbols")
    return [str(item) for item in value]


def _canonical(ctx: RequestContext, symbols: list[str]) -> list[str]:
    """Symbols upper-cased and de-duplicated in order; the first unknown one is refused."""
    canonical: list[str] = []
    for item in symbols:
        if ctx.world.get("tickers", item.upper()) is None:  # raw value echoed (000242, 000718)
            raise _fail(ctx, "missing_reference", "unknown_symbol", param="symbols", value=item)
        if item.upper() not in canonical:
            canonical.append(item.upper())
    return canonical


def _existing_watchlist(ctx: RequestContext, key: int) -> JsonObject:
    record = _find(ctx, "watchlists", key)
    if record is None:
        raise _fail(ctx, "not_found", "watchlist_not_found", value=key)
    return record


async def list_watchlists(ctx: RequestContext) -> Response:
    """Every watchlist, by id (shared by all keys)."""
    items: list[JsonValue] = [_plain(record) for record in ctx.world.list("watchlists")]
    return ctx.respond(items)


async def create_watchlist(ctx: RequestContext) -> Response:
    """201 with the stored record and a Location header; ids count up from 1001."""
    body = _json_object(ctx)
    name = _name(ctx, body)
    symbols = _canonical(ctx, _symbol_list(ctx, body["symbols"])) if "symbols" in body else []
    now = _iso_z(ctx.now_ns, 3)
    record: JsonObject = {
        "id": ctx.world.next_int_key("watchlists", start=1001),
        "name": name,
        "symbols": list(symbols),
        "created_at": now,
        "updated_at": now,
    }
    if (refused := ctx.refuse_missing("watchlists", record)) is not None:
        return refused
    ctx.world.insert("watchlists", record)
    return ctx.respond(record, headers={"location": f"/v1/watchlists/{record['id']}"})


async def get_watchlist(ctx: RequestContext) -> Response:
    """One watchlist."""
    return ctx.respond(_existing_watchlist(ctx, _watchlist_id(ctx)))


async def replace_watchlist(ctx: RequestContext) -> Response:
    """Both fields required; checks: id > body > fields > existence > symbols (000275, 000803)."""
    key = _watchlist_id(ctx)
    body = _json_object(ctx)
    name = _name(ctx, body)
    if "symbols" not in body:
        raise _fail(ctx, "missing_param", "missing_parameter", param="symbols")
    requested = _symbol_list(ctx, body["symbols"])
    record = _existing_watchlist(ctx, key)
    symbols = _canonical(ctx, requested)
    record.update({"name": name, "symbols": list(symbols), "updated_at": _iso_z(ctx.now_ns, 3)})
    ctx.world.replace("watchlists", key, record)
    return ctx.respond(record)


async def update_watchlist(ctx: RequestContext) -> Response:
    """Only the fields present change; ``null`` is refused; ``{}`` still bumps updated_at."""
    key = _watchlist_id(ctx)
    body = _json_object(ctx)
    changes: JsonObject = {}
    if "name" in body:
        changes["name"] = _name(ctx, body)
    requested = _symbol_list(ctx, body["symbols"]) if "symbols" in body else None
    record = _existing_watchlist(ctx, key)  # an unknown id beats an unknown symbol (000802)
    if requested is not None:
        changes["symbols"] = list(_canonical(ctx, requested))
    record.update(changes)
    record["updated_at"] = _iso_z(ctx.now_ns, 3)
    ctx.world.replace("watchlists", key, record)
    return ctx.respond(record)


async def delete_watchlist(ctx: RequestContext) -> Response:
    """204; a second delete is a 404 (000300)."""
    key = _watchlist_id(ctx)
    _existing_watchlist(ctx, key)
    if (refused := ctx.delete("watchlists", key)) is not None:
        return refused
    return ctx.empty(204)


# -- uploads --------------------------------------------------------------------------------------


def upload_view(ctx: RequestContext, record: WorldRecord) -> JsonObject:
    """The upload as of the fake clock: queued 2 s, processing 6 s, then done or failed."""
    upload = _plain(record)
    created_us = _parse_isoformat_us(str(upload["created_at"]))
    elapsed = (ctx.now_ns // 1000 - created_us) / 1_000_000
    view: JsonObject = {key: upload[key] for key in ("id", "status", "filename", "size", "label")}
    view["created_at"] = upload["created_at"]
    if elapsed < QUEUED_S:
        view["status"], view["progress"] = "queued", 0
    elif elapsed < QUEUED_S + PROCESSING_S:
        view["status"] = "processing"
        view["progress"] = int((elapsed - QUEUED_S) / PROCESSING_S * 100)
    else:
        blob = ctx.runtime.blob(ctx, "uploads", record.key)
        failed = blob is not None and b"CORRUPT" in blob.content
        view["status"], view["progress"] = ("failed" if failed else "done"), 100
        view["completed_at"] = _isoformat(created_us + int((QUEUED_S + PROCESSING_S) * 1e6))
        if failed:
            view["error"] = "corrupt_file"
    return view


def _upload(ctx: RequestContext) -> WorldRecord:
    """The upload named in the path: exact id match; malformed ids are not found (000445)."""
    raw = ctx.path_param("id") or ""
    record = ctx.world.get("uploads", raw)
    if record is None:
        raise _fail(ctx, "not_found", "upload_not_found", value=raw)
    return record


async def _uploaded_file(ctx: RequestContext) -> tuple[UploadFile, str | None]:
    """The multipart 'file' part and the optional 'label' ('' -> null, 000435)."""
    media = (ctx.header("content-type") or "").lower()
    if not media.startswith(FORM_TYPES):  # a JSON body or no body has no file (000441, 000442)
        raise _fail(ctx, "missing_param", "missing_parameter", param="file")
    try:
        form = await cast("Request", ctx.request).form(max_part_size=MAX_UPLOAD * 2)
    except (MultiPartException, HTTPException) as broken:  # 000838, 000839
        message = getattr(broken, "message", None) or getattr(broken, "detail", "")
        raise _fail(ctx, "invalid_body", "bad_request", value=str(message)) from None
    field, label = form.get("file"), form.get("label")  # the last 'file' part wins (000808)
    if field is None:
        raise _fail(ctx, "missing_param", "missing_parameter", param="file")
    if not isinstance(field, UploadFile) or not field.filename:  # text, no filename (000807/9)
        raise _fail(ctx, "invalid_param", "invalid_file", param="file")
    return field, (label if isinstance(label, str) and label else None)


async def create_upload(ctx: RequestContext) -> Response:
    """202 queued; the processing outcome is decided by the bytes ('CORRUPT' fails)."""
    upload, label = await _uploaded_file(ctx)
    content = await upload.read()
    if len(content) > MAX_UPLOAD:
        raise _fail(ctx, "payload_too_large", "payload_too_large")
    record: JsonObject = {
        "id": str(uuid.UUID(int=ctx.rng.getrandbits(128), version=4)),
        "status": "queued",
        "filename": upload.filename or "",
        "size": len(content),
        "label": label,
        "created_at": _isoformat(ctx.now_ns // 1000),
        "progress": 0,
    }
    content_type = upload.content_type or "application/octet-stream"
    ctx.runtime.pending_blob(ctx, "file", content, content_type, upload.filename)
    stored = ctx.world.insert("uploads", record)
    ctx.runtime.commit_blob(ctx, "uploads", stored.key)
    return ctx.respond(upload_view(ctx, stored), status=202)


async def list_uploads(ctx: RequestContext) -> Response:
    """Every upload, newest first, each in its current state."""
    records = list(reversed(ctx.world.list("uploads")))
    return ctx.respond([upload_view(ctx, record) for record in records])


async def get_upload(ctx: RequestContext) -> Response:
    """An upload's current status, derived from the time since it was created."""
    return ctx.respond(upload_view(ctx, _upload(ctx)))


async def delete_upload(ctx: RequestContext) -> Response:
    """204 at any state (000448: deleted while queued)."""
    record = _upload(ctx)
    if (refused := ctx.delete("uploads", record.key)) is not None:
        return refused
    return ctx.empty(204)


def _ascii_filename(name: str) -> str:
    """The filename as the real Content-Disposition spells it (000849).

    Non-ASCII characters become '_' and double quotes '%22': 'résumé %22q%22.txt' is sent as
    'r_sum_ %22q%22.txt'.
    """
    return "".join("_" if not char.isascii() else "%22" if char == '"' else char for char in name)


async def download_upload(ctx: RequestContext) -> Response:
    """The original bytes once done; else 409 naming the current status (000386, 000404)."""
    record = _upload(ctx)
    view = upload_view(ctx, record)
    if view["status"] != "done":
        state: dict[str, JsonValue] = {"state": view["status"]}
        raise _fail(ctx, "conflict", "not_ready", value=view["id"], values=state)
    blob = ctx.runtime.blob(ctx, "uploads", record.key)
    content = blob.content if blob is not None else b""
    content_type = (blob.content_type if blob is not None else None) or "application/octet-stream"
    disposition = f'attachment; filename="{_ascii_filename(str(view["filename"]))}"'
    return ctx.respond_bytes(
        content, content_type=content_type, headers={"content-disposition": disposition}
    )


# -- streams --------------------------------------------------------------------------------------

_WALKS: dict[str, list[tuple[str, int]]] = {}


def trade_walk(ctx: RequestContext, symbol: str) -> list[tuple[str, int]]:
    """The symbol's deterministic walk (price, size), from its longest recorded stream."""
    if not _WALKS:
        for exchange in ctx.runtime.recordings.for_endpoint(TRADES):
            requested = exchange.request.query_value("symbol")
            events = [event for event in exchange.stream if event.kind == "sse" and event.data]
            if not requested or not events:
                continue
            steps = []
            for event in events:
                data = json.loads(event.data or "{}")
                steps.append((str(data["price"]), int(data["size"])))
            key = requested.upper()
            if len(steps) > len(_WALKS.get(key, [])):
                _WALKS[key] = steps
    return _WALKS.get(symbol, [])


async def stream_trades(ctx: RequestContext) -> Response:
    """SSE: ``count`` trade events, 200 ms apart; count is checked before symbol (000483)."""
    count = _bounded(ctx, "count", 10, 50, "invalid_count")
    raw_symbol = _last(ctx, "symbol")
    if raw_symbol is None:
        raise _fail(ctx, "missing_param", "missing_parameter", param="symbol")
    symbol = raw_symbol.upper()
    if ctx.world.get("tickers", symbol) is None:  # raw value echoed (000711)
        raise _fail(ctx, "invalid_param", "unknown_symbol", param="symbol", value=raw_symbol)
    walk = trade_walk(ctx, symbol)[:count]

    async def events() -> AsyncIterator[bytes]:
        for index, (price, size) in enumerate(walk, start=1):
            await ctx.pause(FIRST_TRADE_S if index == 1 else TRADE_SPACING_S)
            trade: JsonObject = {
                "symbol": symbol,
                "price": price,
                "size": size,
                "time": _iso_z(_live_ns(ctx), 3),
            }
            yield f"event: trade\nid: {index}\ndata: {_dumps(trade)}\n\n".encode()

    headers = {"cache-control": "no-cache", **ctx.extra_headers}
    return StreamingResponse(
        events(), media_type="text/event-stream; charset=utf-8", headers=headers
    )


_WS_REFUSALS: Final = {
    "missing": "Missing api_key query parameter.",
    "invalid": "Invalid API key.",
    "forbidden": "This API key has been revoked.",
}


async def _ws_error(websocket: WebSocket, message: str) -> None:
    await websocket.send_text(_dumps({"type": "error", "message": message}))


async def _ws_feed(websocket: WebSocket, ctx: RequestContext, symbols: list[str]) -> None:
    """subscribed, 5 rounds of one quote per symbol, done, close 1000 (000486, 000733)."""
    subscribed: list[JsonValue] = list(symbols)
    await websocket.send_text(_dumps({"type": "subscribed", "symbols": subscribed}))
    for round_index in range(WS_ROUNDS):
        if round_index:
            await ctx.pause(WS_SPACING_S)
        now_ns = _live_ns(ctx)
        for symbol in symbols:
            exchange = str(_require(ctx, "tickers", symbol)["exchange"])
            quote: JsonObject = {
                "type": "quote",
                "symbol": symbol,
                "price": _require(ctx, "quotes", symbol)["price"],
                "as_of": quote_state(ctx, exchange, now_ns)[1],
            }
            await websocket.send_text(_dumps(quote))
    await websocket.send_text('{"type":"done"}')
    await websocket.close(code=1000)


def _ws_request(ctx: RequestContext, data: JsonValue) -> str | list[str] | None:
    """One parsed client message: an error message, the symbols to stream, or ``None`` (ping)."""
    if not isinstance(data, dict):
        return "Messages must be JSON objects."
    action = data.get("action")
    if action == "ping":
        return None
    if action != "subscribe":
        return f"Unknown action: {json.dumps(action)}."  # JSON spelling: "dance", 5, null
    symbols = data.get("symbols")
    if not (isinstance(symbols, list) and symbols and all(isinstance(s, str) for s in symbols)):
        return "symbols must be a non-empty array of strings."
    canonical: list[str] = []
    for item in map(str, symbols):
        if ctx.world.get("tickers", item.upper()) is None:
            return f"Unknown symbol '{item}'."
        if item.upper() not in canonical:
            canonical.append(item.upper())
    return canonical


async def _ws_answer(websocket: WebSocket, ctx: RequestContext, text: str) -> bool:
    """Answer one client message; ``True`` once the feed ran (the socket is then closed)."""
    try:
        data = json.loads(text)
    except ValueError:
        await _ws_error(websocket, "Invalid JSON.")
        return False
    request = _ws_request(ctx, data)
    if request is None:
        await websocket.send_text('{"type":"pong"}')
        return False
    if isinstance(request, str):
        await _ws_error(websocket, request)
        return False
    await _ws_feed(websocket, ctx, request)
    return True


async def ws_quotes(websocket: WebSocket, ctx: RequestContext) -> None:
    """Quote feed: api_key in the query; ping/subscribe; errors keep the socket open."""
    api_key = websocket.query_params.get("api_key") or ""
    outcome = ctx.runtime.auth.authenticate(
        headers={"x-api-key": api_key} if api_key else {},
        query={},
        cookies={},
        now_ns=ctx.now_ns,
        required=True,
    )
    await websocket.accept()
    if outcome.failure is not None:  # accepted, then closed with a reason (000501-000505)
        reason = _WS_REFUSALS.get(outcome.failure, _WS_REFUSALS["invalid"])
        await websocket.close(code=4401, reason=reason)
        return
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await _ws_error(websocket, "Binary messages are not supported.")
            elif await _ws_answer(websocket, ctx, message.get("text") or ""):
                return
    except WebSocketDisconnect:
        return


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
        TRADES: stream_trades,
    }
)
WEBSOCKET_HANDLERS["GET /v1/ws/quotes"] = ws_quotes
