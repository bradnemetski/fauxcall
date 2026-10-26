"""An in-process MCP session against the fake, with its clock under the test's control."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import httpx2
    from fastapi import FastAPI
    from fauxcall.types import JsonObject, JsonValue


@dataclass
class Session:
    """One connected client: call the server, hear its stream, move its clock."""

    app: FastAPI
    http: httpx2.AsyncClient
    wire: McpWireClient
    heard_positions: set[int] = field(default_factory=set)

    async def call(self, method: str, params: JsonValue = None) -> Any:
        """The answer: the result, else the error."""
        reply = (await self.wire.call(method, params)).response
        assert isinstance(reply, dict)
        return reply.get("result", reply.get("error"))

    async def messages(self, method: str, params: JsonValue = None) -> list[Any]:
        """Everything the server sent while answering (requests, notifications, the answer)."""
        return (await self.wire.call(method, params)).messages

    async def tool(self, name: str, arguments: JsonObject | None = None, **extra: JsonValue) -> Any:
        """A tool call's answer."""
        params: JsonObject = {"name": name, "arguments": arguments or {}, **extra}
        return await self.call("tools/call", params)

    def answer(self, replies: dict[str, Any] | Callable[[JsonObject], Awaitable[Any]]) -> None:
        """How the client answers the server's requests from now on."""
        self.wire.on_request = replies if callable(replies) else replier(replies)

    async def heard(self, method: str, count: int = 1, within: float = 3.0) -> list[Any]:
        """The next ``count`` messages of ``method`` on the client's GET stream."""
        found = await self.wire.heard(
            lambda message: isinstance(message, dict) and message.get("method") == method,
            count=count,
            within=within,
            taken=self.heard_positions,
        )
        self.heard_positions.update(position for position, _ in found)
        return [message for _, message in found]

    async def clock(self, op: str, **values: float) -> None:
        """Move the fake's clock (``freeze``, ``advance`` …) and let waiting work run."""
        response = await self.http.post("/__fauxcall/clock", json={"op": op, **values})
        assert response.status_code == 200, response.text
        for _ in range(20):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        """Advance the fake's clock."""
        await self.clock("advance", seconds=seconds)


@contextlib.asynccontextmanager
async def session(
    capabilities: JsonObject | None = None,
    replies: dict[str, Any] | None = None,
    *,
    listen: bool = False,
    frozen: bool = False,
) -> AsyncIterator[Session]:
    """A fresh fake and one client session on it."""
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        wire.on_request = replier(replies or {})
        opened = Session(app, http, wire)
        if frozen:
            await opened.clock("freeze")
        await wire.initialize(capabilities=capabilities)
        if listen:
            await wire.listen()
        try:
            yield opened
        finally:
            await wire.close()


def texts(result: Any) -> list[str]:
    """The texts of a tool result."""
    assert isinstance(result, dict), result
    return [str(block.get("text", "")) for block in result["content"]]
