"""The MCP server (official ``mcp`` SDK v2, ``MCPServer``), served at ``/mcp`` over Streamable HTTP.

It shares the REST API's catalog, clock and presenters, so a tool result always equals the
corresponding REST response. MCP requests are not authenticated, rate limited or delayed.

Tools, the prompt and the resource template are plain module-level functions. Each app's
:class:`AppContext` reaches them through the MCP server's lifespan context (the SDK's dependency
injection), so the SDK's per-function metadata is built once per process and shared by every app.
"""

from __future__ import annotations

import functools
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any, Final

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.server.mcpserver.prompts import Prompt
from mcp.server.mcpserver.tools import Tool
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, ToolAnnotations
from pydantic import Field, JsonValue

from fauxcall_reference_api import API_VERSION
from fauxcall_reference_api.context import AppContext
from fauxcall_reference_api.schemas import MarketStatusOut, QuoteOut, SearchOut
from fauxcall_reference_api.search import MAX_RESULTS, tokenize
from fauxcall_reference_api.simulation import format_price
from fauxcall_reference_api.views import exchange_out, market_status_out, quote_out, search_out

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from fauxcall_reference_api.catalog import Catalog, Exchange, Ticker

SERVER_NAME: Final = "fauxcall-reference-api"
INSTRUCTIONS: Final = (
    "FauxCall reference market-data server with deterministic test data. Use search_tickers to "
    "find symbols, get_quote for a ticker's quote (static price; the timestamp follows the "
    "exchange's trading sessions), market_status for whether an exchange is open with its next "
    "open and last close, and explain_ticker for a short templated answer about a ticker. "
    "Exchange details are resources at exchange://{code} (XHKG, XLON, XNAS, XNYS, XPAR, XTKS)."
)
MIN_QUERY_LENGTH: Final = 2
MAX_QUERY_LENGTH: Final = 64

# Checked in order; the first topic with a keyword in the question wins, else "general".
TOPIC_KEYWORDS: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    ("price", frozenset({"price", "cost", "worth", "value", "valuation", "expensive", "cheap"})),
    ("risk", frozenset({"risk", "risky", "volatile", "volatility", "safe", "safety"})),
    ("listing", frozenset({"listed", "listing", "ipo", "exchange", "history", "since", "public"})),
    ("business", frozenset({"business", "sector", "industry", "products", "do", "does", "make"})),
)

_READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)
_LIMIT_SCHEMA: Final[dict[str, JsonValue]] = {"minimum": 1, "maximum": MAX_RESULTS}


def mcp_function[F: Callable[..., object]](fn: F) -> F:
    """Mark a function whose annotations the MCP SDK evaluates at runtime (identity decorator)."""
    return fn


def question_topic(question: str) -> str:
    """Classify a question as ``price``, ``risk``, ``listing``, ``business`` or ``general``."""
    words = set(tokenize(question))
    for topic, keywords in TOPIC_KEYWORDS:
        if words & keywords:
            return topic
    return "general"


def _session_hours(exchange: Exchange) -> str:
    spans = [f"{s.open:%H:%M}-{s.close:%H:%M}" for s in exchange.sessions]
    return " and ".join(spans)


def explain(catalog: Catalog, ticker: Ticker, question: str) -> str:
    """The deterministic answer for ``(ticker, topic of question)``; the question is not echoed."""
    exchange = catalog.exchange_of(ticker)
    price = format_price(ticker.price)
    intro = (
        f"{ticker.name} ({ticker.symbol}) is listed on the {exchange.name} ({exchange.code}) "
        f"in the {ticker.sector} sector and trades in {ticker.currency}."
    )
    details = {
        "price": f"Its reference price is {price} {ticker.currency}; this API never changes "
        "prices, so every quote reports the same value.",
        "risk": f"As a {ticker.sector} stock it carries sector-specific risk; simulated trades "
        f"move at most 0.10% per trade around {price} {ticker.currency}.",
        "listing": f"It has been listed since {ticker.listed_on.isoformat()} and trades during "
        f"{exchange.code} sessions ({_session_hours(exchange)} {exchange.timezone}).",
        "business": f"Its business is classified in the {ticker.sector} sector; search for "
        f"'{ticker.sector}' to find its peers.",
        "general": "Ask about its price, risk, listing or business for more detail.",
    }
    return f"{intro} {details[question_topic(question)]}"


def require_ticker(catalog: Catalog, symbol: str) -> Ticker:
    """Look a ticker up or fail the tool call."""
    ticker = catalog.ticker(symbol)
    if ticker is None:
        raise ToolError(f"Unknown symbol '{symbol}'.")
    return ticker


def require_exchange(catalog: Catalog, code: str) -> Exchange:
    """Look an exchange up or fail the tool call."""
    exchange = catalog.exchange(code)
    if exchange is None:
        raise ToolError(f"Unknown exchange '{code}'.")
    return exchange


def check_search_arguments(query: str, limit: int) -> None:
    """Fail the tool call unless ``2 <= len(query) <= 64`` and ``1 <= limit <= 10``."""
    if not MIN_QUERY_LENGTH <= len(query) <= MAX_QUERY_LENGTH:
        raise ToolError("query must be 2 to 64 characters.")
    if not 1 <= limit <= MAX_RESULTS:
        raise ToolError("limit must be between 1 and 10.")


def _app(ctx: Context[AppContext, Any]) -> AppContext:
    return ctx.request_context.lifespan_context


@mcp_function
def get_quote(
    symbol: Annotated[str, Field(description="Ticker symbol, case-insensitive (e.g. AAPL).")],
    ctx: Context[AppContext, Any],
) -> QuoteOut:
    """Get a ticker's quote: static price, currency, market state and as-of timestamp."""
    app = _app(ctx)
    return quote_out(app.catalog, require_ticker(app.catalog, symbol), app.now_ns())


@mcp_function
def search_tickers(
    query: Annotated[str, Field(description="Free text, 2-64 characters.")],
    ctx: Context[AppContext, Any],
    limit: Annotated[
        int, Field(description="Maximum results, 1-10.", json_schema_extra=_LIMIT_SCHEMA)
    ] = 5,
) -> SearchOut:
    """Search tickers by symbol, name or sector (synonyms like 'car' or 'chips' work)."""
    check_search_arguments(query, limit)
    return search_out(_app(ctx).catalog, query, limit)


@mcp_function
def market_status(
    exchange: Annotated[str, Field(description="Exchange code, case-insensitive (e.g. XNYS).")],
    ctx: Context[AppContext, Any],
) -> MarketStatusOut:
    """Whether an exchange is open, when its session closes, its next open and last close."""
    app = _app(ctx)
    return market_status_out(require_exchange(app.catalog, exchange), app.now_ns())


@mcp_function
def explain_ticker(
    symbol: Annotated[str, Field(description="Ticker symbol, case-insensitive.")],
    question: Annotated[str, Field(description="A free-text question about the ticker.")],
    ctx: Context[AppContext, Any],
) -> str:
    """Answer a question about a ticker with a short deterministic paragraph."""
    catalog = _app(ctx).catalog
    return explain(catalog, require_ticker(catalog, symbol), question)


@mcp_function
def exchange_resource(code: str, ctx: Context[AppContext, Any]) -> str:
    """The ``exchange://{code}`` body (same JSON as REST), or a not-found resource error."""
    exchange = _app(ctx).catalog.exchange(code)
    if exchange is None:
        raise ResourceNotFoundError(f"Unknown exchange '{code}'.")
    return exchange_out(exchange).model_dump_json()


@mcp_function
def analyze_ticker(symbol: str, ctx: Context[AppContext, Any]) -> str:
    """A prompt asking the model to analyze one ticker with this server's tools."""
    catalog = _app(ctx).catalog
    ticker = catalog.ticker(symbol)
    if ticker is None:
        raise MCPError(code=INVALID_PARAMS, message=f"Unknown symbol '{symbol}'.")
    exchange = catalog.exchange_of(ticker)
    return (
        f"Analyze {ticker.name} ({ticker.symbol}), listed on the {exchange.name} "
        f"({exchange.code}) in the {ticker.sector} sector. Call get_quote for its quote and "
        f"market_status for {exchange.code} trading hours, then summarize price, market state "
        "and sector context in three bullet points."
    )


@functools.cache
def shared_tools() -> tuple[Tool, ...]:
    """The tool definitions, built once per process."""
    functions = (get_quote, search_tickers, market_status, explain_ticker)
    return tuple(Tool.from_function(fn, annotations=_READ_ONLY) for fn in functions)


@functools.cache
def shared_prompt() -> Prompt:
    """The ``analyze_ticker`` prompt definition, built once per process."""
    return Prompt.from_function(analyze_ticker, title="Analyze a ticker")


def build_mcp_server(context: AppContext) -> MCPServer[AppContext]:
    """Create an MCP server whose tools see ``context`` (via the server lifespan)."""

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
        yield context

    server: MCPServer[AppContext] = MCPServer(
        SERVER_NAME,
        title="FauxCall Reference Market MCP",
        version=API_VERSION,
        instructions=INSTRUCTIONS,
        tools=list(shared_tools()),
        log_level=context.settings.log_level,
        lifespan=lifespan,
    )
    server.resource(
        "exchange://{code}",
        name="exchange",
        title="Exchange",
        description="An exchange as JSON (same body as GET /v1/exchanges/{code}).",
        mime_type="application/json",
    )(exchange_resource)
    server.add_prompt(shared_prompt())
    return server
