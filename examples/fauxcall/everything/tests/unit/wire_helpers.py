"""In-process MCP clients for the unit tests: sessions with capabilities, loose replies."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.testing.asgi import streaming_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from fauxcall.types import JsonObject, JsonValue


@contextlib.asynccontextmanager
async def client(
    app: FastAPI,
    capabilities: JsonObject | None = None,
    replies: dict[str, Any] | None = None,
    *,
    listen: bool = True,
) -> AsyncIterator[McpWireClient]:
    async with streaming_client(app) as http:
        wire = McpWireClient(f"{http.base_url}/mcp", http)
        wire.on_request = replier(replies or {})
        await wire.initialize(capabilities=capabilities)
        if listen:
            await wire.listen()
        try:
            yield wire
        finally:
            await wire.close()


@dataclass(frozen=True, slots=True)
class Reply:
    """A JSON-RPC answer, loosely typed for assertions."""

    result: Any
    error: Any


async def rpc(wire: McpWireClient, method: str, params: JsonValue = None) -> Reply:
    call = await wire.call(method, params)
    return Reply(call.result, call.error)


async def heard(wire: McpWireClient, wait: float = 0.05) -> list[Any]:
    """What the GET stream received (after ``wait`` seconds), with the stream closed."""
    await asyncio.sleep(wait)
    call = await wire.stop_listening()
    assert call is not None
    return [m for m in call.messages if isinstance(m, dict)]


def methods(messages: list[Any]) -> list[str]:
    return [str(m.get("method")) for m in messages]


async def tool(wire: McpWireClient, name: str, arguments: JsonObject | None = None) -> Any:
    reply = await rpc(wire, "tools/call", {"name": name, "arguments": arguments or {}})
    assert isinstance(reply.result, dict), reply.error
    return reply.result
