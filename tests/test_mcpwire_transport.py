"""In-process MCP servers for the wire-level tests, and tests of the streaming transport
(:mod:`fauxcall.testing.asgi`) that serves them.

The servers: FauxCall's own fake (:class:`~fauxcall.runtime.mcp_server.McpFake`) and the official
MCP SDK (``mcp`` v2 ``MCPServer``) over Streamable HTTP.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route

from fauxcall.config import FakeSettings
from fauxcall.model import ApiModel, McpModel, McpToolModel, McpTransportModel, RealTarget
from fauxcall.model.api import SourceInfo
from fauxcall.runtime import FakeRuntime, create_app, register_tool
from fauxcall.testing.asgi import StreamingASGITransport, streaming_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
    from pathlib import Path

    from fastapi import FastAPI
    from starlette.requests import Request

    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

    Message = MutableMapping[str, Any]
    App = Callable[
        [Message, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]],
        Awaitable[None],
    ]

RECORDED = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


# -- FauxCall's fake MCP server --------------------------------------------------------------------

ECHO: JsonObject = {
    "name": "echo",
    "description": "Echo the text back.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    "annotations": {"readOnlyHint": True},
}
DELETE_ITEM: JsonObject = {
    "name": "delete_item",
    "description": "Delete an item.",
    "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}}},
}
WHOAMI: JsonObject = {
    "name": "whoami",
    "description": "Who is calling.",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"readOnlyHint": False},
}


def fake_model(
    *,
    transport: McpTransportModel | None = None,
    tools: list[JsonObject] | None = None,
    page_size: int | None = None,
    **mcp: Any,
) -> ApiModel:
    """A small MCP model: echo (read-only), delete_item and whoami (may modify state)."""
    definitions = tools if tools is not None else [ECHO, DELETE_ITEM, WHOAMI]
    return ApiModel(
        name="demo",
        title="Demo",
        kind="mcp",
        source=SourceInfo(kind="mcp", url="https://mcp.demo.test/mcp", fetched_at=RECORDED),
        real=RealTarget(base_url="https://mcp.demo.test"),
        mcp=McpModel(
            transport=transport or McpTransportModel(response_mode="json"),
            initialize_result={
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}, "prompts": {}},
                "serverInfo": {"name": "Demo", "version": "1.0.0"},
            },
            tools=[McpToolModel(definition=d, behavior="computed") for d in definitions],
            tools_page_size=page_size,
            prompts=[{"name": "summarize", "description": "Summarize a topic."}],
            **mcp,
        ),
    )


async def _echo(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    del ctx
    return {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]}


async def _delete(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    del ctx
    return {"content": [{"type": "text", "text": f"deleted {arguments.get('item_id')}"}]}


async def _whoami(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    del ctx, arguments
    return {
        "content": [{"type": "text", "text": "jane.roe@example.org"}],
        "structuredContent": {"email": "jane.roe@example.org", "api_key": "sk-live-1234567890"},
    }


def fake_app(tmp_path: Path, model: ApiModel | None = None) -> FastAPI:
    """FauxCall's fake for ``model`` (default :func:`fake_model`), tools computed in-process."""
    model = model or fake_model()
    settings = FakeSettings(data_dir=tmp_path / "data", log_level="WARNING", fast=True)
    app = create_app(settings, runtime=FakeRuntime(settings, model), configure_logs=False)
    handlers = {"echo": _echo, "delete_item": _delete, "whoami": _whoami}
    for tool in model.mcp.tools if model.mcp else []:
        if tool.name in handlers:
            register_tool(app, tool.name, handlers[tool.name])
    return app


# -- the official MCP SDK --------------------------------------------------------------------------


def sdk_server() -> MCPServer:
    """An official SDK server: a read-only tool, a mutating tool, a resource and a prompt."""
    server = MCPServer("sdk-demo", version="2.0.0", instructions="SDK demo", log_level="WARNING")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def echo(text: str) -> str:
        """Echo text."""
        return text

    @server.tool()
    def delete_item(item_id: int) -> str:
        """Delete an item."""
        return f"deleted {item_id}"

    @server.resource("demo://greeting")
    def greeting() -> str:
        """A greeting."""
        return "hello"

    @server.prompt()
    def summarize(topic: str) -> str:
        """Summarize a topic."""
        return f"Summarize {topic}"

    return server


@asynccontextmanager
async def sdk_client(
    *, json_response: bool = False, stateless: bool = False
) -> AsyncIterator[httpx2.AsyncClient]:
    """A client wired to a running SDK server at ``http://sdk.test/mcp``."""
    server = sdk_server()
    app = server.streamable_http_app(
        json_response=json_response,
        stateless_http=stateless,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    async with server.session_manager.run(), streaming_client(app, "http://sdk.test") as http:
        yield http


# -- tests of the transport itself -----------------------------------------------------------------


async def _ticks(request: Request) -> StreamingResponse:
    async def body() -> AsyncIterator[bytes]:
        for index in range(3):
            yield f"tick {index}\n".encode()
            await asyncio.sleep(0)
        if request.query_params.get("forever"):
            while True:
                await asyncio.sleep(3600)
                yield b"never\n"  # pragma: no cover - the client hangs up first

    return StreamingResponse(body(), media_type="text/plain")


async def _boom(request: Request) -> PlainTextResponse:
    del request
    raise RuntimeError("boom")


_TEST_APP = Starlette(routes=[Route("/ticks", _ticks), Route("/boom", _boom)])


async def test_streams_chunks_incrementally_and_hangs_up_cleanly() -> None:
    async with streaming_client(_TEST_APP) as http:
        async with http.stream("GET", "/ticks") as response:
            chunks = [chunk async for chunk in response.aiter_bytes()]
        assert chunks == [b"tick 0\n", b"tick 1\n", b"tick 2\n"]
        async with http.stream("GET", "/ticks", params={"forever": "1"}) as response:
            seen = []
            async for chunk in response.aiter_bytes():
                seen.append(chunk)
                if len(seen) == 2:
                    break
        assert seen == [b"tick 0\n", b"tick 1\n"]


async def test_app_errors_reach_the_client() -> None:
    async with streaming_client(_TEST_APP) as http:
        assert (await http.get("/boom")).status_code == 500  # Starlette answers, then re-raises

    async def broken(scope: Message, receive: Any, send: Any) -> None:
        del receive
        if scope["path"] == "/before":
            raise ValueError("before the response")
        if scope["path"] == "/silent":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"part", "more_body": True})
        if scope["path"] == "/during":
            raise ValueError("during the body")

    async with httpx2.AsyncClient(
        transport=StreamingASGITransport(broken), base_url="http://x"
    ) as http:
        with pytest.raises(ValueError, match="before the response"):
            await http.get("/before")
        with pytest.raises(RuntimeError, match="without starting a response"):
            await http.get("/silent")
        with pytest.raises(ValueError, match="during the body"):
            await http.get("/during")
        assert (await http.get("/unfinished")).content == b"part"


async def test_a_stuck_app_is_cancelled_when_the_client_hangs_up() -> None:
    started = asyncio.Event()

    async def stuck(scope: Message, receive: Any, send: Any) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        await asyncio.sleep(3600)

    transport = StreamingASGITransport(stuck, close_timeout=0.01)
    async with httpx2.AsyncClient(transport=transport, base_url="http://x") as http:
        async with http.stream("GET", "/") as response:
            assert response.status_code == 200
        assert started.is_set()


async def test_the_helpers_serve_both_mcp_servers(tmp_path: Path) -> None:
    async with streaming_client(fake_app(tmp_path)) as http:
        response = await http.get("/mcp")
        assert response.status_code == 405
    async with sdk_client() as http:
        response = await http.get("/mcp")
        assert response.status_code in {400, 406}
