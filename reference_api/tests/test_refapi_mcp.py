from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from typing import TYPE_CHECKING

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import TextContent, TextResourceContents

from fauxcall_reference_api.app import create_app
from fauxcall_reference_api.catalog import default_catalog
from fauxcall_reference_api.mcp_server import INSTRUCTIONS, explain, question_topic
from fauxcall_reference_api.settings import Settings

from .helpers import DEMO, at

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from mcp.server import MCPServer

    from fauxcall_reference_api.clock import ManualClock

CATALOG = default_catalog()


def server_of(app: FastAPI) -> MCPServer:
    server: MCPServer = app.state.mcp
    return server


def text_of(result: object) -> str:
    content = getattr(result, "content", None)
    assert isinstance(content, list)
    (block,) = content
    assert isinstance(block, TextContent)
    return block.text


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_server_metadata(app: FastAPI, mode: str) -> None:
    async with Client(server_of(app), mode=mode) as client:
        assert client.instructions == INSTRUCTIONS
        info = client.server_info
        assert info is not None
        assert (info.name, info.version) == ("fauxcall-reference-api", "1.0.0")
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert sorted(tools) == ["explain_ticker", "get_quote", "market_status", "search_tickers"]
        assert tools["search_tickers"].input_schema["properties"]["limit"]["maximum"] == 10
        assert tools["get_quote"].annotations is not None
        assert tools["get_quote"].annotations.read_only_hint is True
        assert tools["get_quote"].output_schema is not None
        (template,) = (await client.list_resource_templates()).resource_templates
        assert (template.uri_template, template.mime_type) == (
            "exchange://{code}",
            "application/json",
        )
        (prompt,) = (await client.list_prompts()).prompts
        assert prompt.name == "analyze_ticker"
        assert prompt.arguments is not None
        assert [(a.name, a.required) for a in prompt.arguments] == [("symbol", True)]


async def test_get_quote_matches_rest(app: FastAPI, clock: ManualClock) -> None:
    from starlette.testclient import TestClient  # noqa: PLC0415 - sync client inside an async test

    clock.set_ns(at(2026, 10, 9, 11, 0, tz="America/New_York"))
    rest = TestClient(app).get("/v1/tickers/AAPL/quote", headers=DEMO).json()
    async with Client(server_of(app)) as client:
        result = await client.call_tool("get_quote", {"symbol": "aapl"})
    assert not result.is_error
    assert result.structured_content == rest
    assert json.loads(text_of(result)) == rest


async def test_tool_errors(app: FastAPI) -> None:
    async with Client(server_of(app)) as client:
        cases: list[tuple[str, dict[str, object], str]] = [
            ("get_quote", {"symbol": "NOPE"}, "Unknown symbol 'NOPE'."),
            ("market_status", {"exchange": "nope"}, "Unknown exchange 'nope'."),
            ("explain_ticker", {"symbol": "nope", "question": "?"}, "Unknown symbol 'nope'."),
            ("search_tickers", {"query": "a"}, "query must be 2 to 64 characters."),
            ("search_tickers", {"query": "car", "limit": 11}, "limit must be between 1 and 10."),
            ("search_tickers", {"query": "car", "limit": 0}, "limit must be between 1 and 10."),
        ]
        for name, arguments, message in cases:
            result = await client.call_tool(name, arguments)
            assert result.is_error
            assert text_of(result) == f"Error executing tool {name}: {message}"


async def test_search_tickers_tool(app: FastAPI) -> None:
    async with Client(server_of(app)) as client:
        result = await client.call_tool("search_tickers", {"query": "chips"})
        assert result.structured_content == {
            "query": "chips",
            "results": [
                {
                    "symbol": "0981.HK",
                    "name": "Semiconductor Manufacturing International",
                    "score": 0.5,
                },
                {"symbol": "8035.T", "name": "Tokyo Electron Limited", "score": 0.5},
                {"symbol": "AMD", "name": "Advanced Micro Devices, Inc.", "score": 0.5},
                {"symbol": "AVGO", "name": "Broadcom Inc.", "score": 0.5},
                {"symbol": "INTC", "name": "Intel Corporation", "score": 0.5},
            ],
        }
        ten = await client.call_tool("search_tickers", {"query": "inc", "limit": 10})
        assert ten.structured_content is not None
        assert len(ten.structured_content["results"]) == 10


async def test_market_status_tool(app: FastAPI, clock: ManualClock) -> None:
    clock.set_ns(at(2026, 11, 27, 12, 0, tz="America/New_York"))
    async with Client(server_of(app)) as client:
        result = await client.call_tool("market_status", {"exchange": "xnys"})
    assert result.structured_content == {
        "exchange": "XNYS",
        "name": "New York Stock Exchange",
        "timezone": "America/New_York",
        "market_state": "open",
        "session_closes_at": "2026-11-27T18:00:00Z",
        "next_open": "2026-11-30T14:30:00Z",
        "last_close": "2026-11-25T21:00:00Z",
    }


@pytest.mark.parametrize(
    ("question", "topic"),
    [
        ("What is the price?", "price"),
        ("Is it expensive right now", "price"),
        ("how risky is this stock", "risk"),
        ("When did it IPO?", "listing"),
        ("Which exchange lists it?", "listing"),
        ("What does the company do", "business"),
        ("Tell me something", "general"),
        ("Is the price risky?", "price"),  # first topic in order wins
    ],
)
def test_question_topic(question: str, topic: str) -> None:
    assert question_topic(question) == topic


async def test_explain_ticker_tool(app: FastAPI) -> None:
    async with Client(server_of(app)) as client:
        first = await client.call_tool(
            "explain_ticker", {"symbol": "7203.t", "question": "When was it listed?"}
        )
        second = await client.call_tool(
            "explain_ticker", {"symbol": "7203.T", "question": "listing history please"}
        )
    expected = (
        "Toyota Motor Corporation (7203.T) is listed on the Tokyo Stock Exchange (XTKS) in the "
        "Automotive sector and trades in JPY. It has been listed since 1949-05-16 and trades "
        "during XTKS sessions (09:00-11:30 and 12:30-15:30 Asia/Tokyo)."
    )
    assert text_of(first) == expected
    assert text_of(second) == expected
    assert first.structured_content == {"result": expected}


def test_explain_every_topic() -> None:
    ticker = CATALOG.ticker("AAPL")
    assert ticker is not None
    intro = (
        "Apple Inc. (AAPL) is listed on the Nasdaq Stock Market (XNAS) in the Technology sector "
        "and trades in USD."
    )
    assert explain(CATALOG, ticker, "price?") == (
        f"{intro} Its reference price is 227.48 USD; this API never changes prices, so every quote "
        "reports the same value."
    )
    assert explain(CATALOG, ticker, "risk?") == (
        f"{intro} As a Technology stock it carries sector-specific risk; simulated trades move at "
        "most 0.10% per trade around 227.48 USD."
    )
    assert explain(CATALOG, ticker, "what does it do") == (
        f"{intro} Its business is classified in the Technology sector; search for 'Technology' to "
        "find its peers."
    )
    assert explain(CATALOG, ticker, "hello") == (
        f"{intro} Ask about its price, risk, listing or business for more detail."
    )


async def test_exchange_resource(app: FastAPI) -> None:
    async with Client(server_of(app)) as client:
        result = await client.read_resource("exchange://xhkg")
        (contents,) = result.contents
        assert isinstance(contents, TextResourceContents)
        assert contents.mime_type == "application/json"
        assert json.loads(contents.text) == {
            "code": "XHKG",
            "name": "Hong Kong Stock Exchange",
            "mic": "XHKG",
            "timezone": "Asia/Hong_Kong",
            "currency": "HKD",
            "sessions": [{"open": "09:30", "close": "12:00"}, {"open": "13:00", "close": "16:00"}],
        }
        with pytest.raises(MCPError, match="Unknown exchange 'NOPE'") as missing:
            await client.read_resource("exchange://NOPE")
        assert (missing.value.code, missing.value.message) == (-32602, "Unknown exchange 'NOPE'.")


async def test_analyze_prompt(app: FastAPI) -> None:
    async with Client(server_of(app)) as client:
        result = await client.get_prompt("analyze_ticker", {"symbol": "shel.l"})
        (message,) = result.messages
        assert message.role == "user"
        assert isinstance(message.content, TextContent)
        assert message.content.text == (
            "Analyze Shell plc (SHEL.L), listed on the London Stock Exchange (XLON) in the Energy "
            "sector. Call get_quote for its quote and market_status for XLON trading hours, then "
            "summarize price, market state and sector context in three bullet points."
        )
        with pytest.raises(MCPError, match="Unknown symbol 'NOPE'") as missing:
            await client.get_prompt("analyze_ticker", {"symbol": "NOPE"})
        assert (missing.value.code, missing.value.message) == (-32602, "Unknown symbol 'NOPE'.")


class _Server(uvicorn.Server):
    """A uvicorn server that signals when it is ready to accept connections."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        self.ready.set()


@contextlib.asynccontextmanager
async def running_server(app: FastAPI) -> AsyncIterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = _Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await asyncio.wait_for(server.ready.wait(), timeout=10)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_over_streamable_http(mode: str) -> None:
    app = create_app(Settings(latency=False))
    async with running_server(app) as base_url:
        async with httpx2.AsyncClient(base_url=base_url) as http:
            assert (await http.get("/healthz")).json() == {"status": "ok"}
            quote = (await http.get("/v1/tickers/MSFT/quote", headers=DEMO)).json()
            assert quote["price"] == "415.26"
            not_slash = await http.get("/mcp/", follow_redirects=False)
            assert not_slash.status_code == 404
        async with Client(f"{base_url}/mcp", mode=mode) as client:
            assert client.instructions == INSTRUCTIONS
            result = await client.call_tool("get_quote", {"symbol": "MSFT"})
            assert result.structured_content is not None
            assert result.structured_content["price"] == "415.26"
            assert result.structured_content["symbol"] == "MSFT"
