"""In-process MCP servers for the wire-level tests, and tests of the transport that serves them.

``httpx2.ASGITransport`` buffers a whole response before returning it, which never finishes for
an endless SSE stream (an MCP ``GET`` stream, the legacy HTTP+SSE transport).
:class:`StreamingASGITransport` streams bodies chunk by chunk instead and, like a real server,
tells the app when the client hangs up.

The servers: FauxCall's own fake (:class:`~fauxcall.runtime.mcp_server.McpFake`) and the official
MCP SDK (``mcp`` v2 ``MCPServer``) over Streamable HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
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


class _Body(httpx2.AsyncByteStream):
    def __init__(
        self,
        messages: asyncio.Queue[Message],
        disconnected: asyncio.Event,
        task: asyncio.Task[None],
        close_timeout: float,
    ) -> None:
        self._messages = messages
        self._disconnected = disconnected
        self._task = task
        self._close_timeout = close_timeout

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            message = await self._messages.get()
            if message["type"] == "http.response.body":
                if message.get("body"):
                    yield bytes(message["body"])
                if not message.get("more_body", False):
                    return
            elif message["type"] == "app.error":
                raise message["error"]
            elif message["type"] == "app.done":
                return

    async def aclose(self) -> None:
        self._disconnected.set()
        if self._task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), self._close_timeout)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class StreamingASGITransport(httpx2.AsyncBaseTransport):
    """Serve an ASGI app in-process with streamed response bodies (see the module docstring)."""

    def __init__(self, app: Any, *, close_timeout: float = 5.0) -> None:
        self.app: App = app
        self.close_timeout = close_timeout

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        scope: Message = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 123),
            "root_path": "",
        }
        messages: asyncio.Queue[Message] = asyncio.Queue()
        disconnected = asyncio.Event()
        delivered = False

        async def receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            await messages.put(message)

        async def run() -> None:
            try:
                await self.app(scope, receive, send)
            except Exception as error:  # noqa: BLE001 - handed to the client like a server would
                await messages.put({"type": "app.error", "error": error})
            finally:
                await messages.put({"type": "app.done"})

        task = asyncio.create_task(run())
        start = await messages.get()
        if start["type"] != "http.response.start":
            await task
            if start["type"] == "app.error":
                raise start["error"]
            raise RuntimeError("the app returned without starting a response")
        stream = _Body(messages, disconnected, task, self.close_timeout)
        return httpx2.Response(start["status"], headers=start.get("headers", []), stream=stream)


def streaming_client(app: Any, base_url: str = "http://fake.test") -> httpx2.AsyncClient:
    """An in-process client for ``app`` that streams responses."""
    return httpx2.AsyncClient(transport=StreamingASGITransport(app), base_url=base_url)


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
