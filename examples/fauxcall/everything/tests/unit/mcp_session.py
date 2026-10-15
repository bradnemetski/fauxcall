"""In-process MCP sessions with the fake, as a client declaring capabilities and answering."""

from __future__ import annotations

import base64
import binascii
import contextlib
import re
from typing import TYPE_CHECKING, Final

from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.testing.asgi import streaming_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from fastapi import FastAPI
    from fauxcall.types import JsonObject, JsonValue

_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TASK_ID: Final = re.compile(r"\b[0-9a-f]{32}\b")
_ISO: Final = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
_LOCAL_TIME: Final = re.compile(r"created at \d{1,2}:\d\d:\d\d [AP]M")


@contextlib.asynccontextmanager
async def connect(
    app: FastAPI,
    *,
    capabilities: JsonObject | None = None,
    replies: Mapping[str, JsonValue] | None = None,
    listen: bool = False,
) -> AsyncIterator[McpWireClient]:
    """A session with the fake: initialized, answering the server's requests with ``replies``."""
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize(capabilities=capabilities)
        wire.on_request = replier(replies or {})
        if listen:
            await wire.listen()
        try:
            yield wire
        finally:
            await wire.close()


async def answer(wire: McpWireClient, method: str, params: JsonValue = None) -> JsonValue:
    """The ``result`` (or ``error``) of one request."""
    call = await wire.call(method, params)
    reply = call.response if isinstance(call.response, dict) else {}
    return reply.get("result", reply.get("error"))


async def tool(
    wire: McpWireClient, name: str, arguments: JsonValue = None, **extra: JsonValue
) -> JsonObject:
    """The answer of one tool call (its result, or its JSON-RPC error)."""
    params: JsonObject = {"name": name, "arguments": arguments if arguments is not None else {}}
    params.update(extra)
    found = await answer(wire, "tools/call", params)
    assert isinstance(found, dict)
    return found


def dig(value: JsonValue, *path: str | int) -> JsonValue:
    """A member deep inside an answer (asserting each step exists)."""
    for step in path:
        if isinstance(step, int):
            assert isinstance(value, list)
            value = value[step]
        else:
            assert isinstance(value, dict)
            value = value[step]
    return value


def text_of(result: JsonObject, index: int = 0) -> str:
    """The text of one content block."""
    content = result["content"]
    assert isinstance(content, list)
    block = content[index]
    assert isinstance(block, dict)
    return str(block["text"])


def normalized(value: JsonValue) -> JsonValue:
    """An answer with what legitimately differs run to run replaced by placeholders.

    UUIDs (session and elicitation ids), task ids, ISO timestamps and the local time stamped
    on dynamic resources (also inside their base64 blobs).
    """
    if isinstance(value, str):
        value = _UUID.sub("<uuid>", value)
        value = _TASK_ID.sub("<task>", value)
        value = _ISO.sub("<iso>", value)
        return _LOCAL_TIME.sub("created at <time>", value)
    if isinstance(value, list):
        return [normalized(item) for item in value]
    if isinstance(value, dict):
        found: JsonObject = {}
        for key, item in value.items():
            if key == "blob" and isinstance(item, str):
                found[key] = _blob(item)
            else:
                found[key] = normalized(item)
        return found
    return value


def _blob(blob: str) -> JsonValue:
    """A dynamic resource's blob is its stamped text: compare the text, without the time."""
    try:
        text = base64.b64decode(blob).decode()
    except (binascii.Error, UnicodeDecodeError):
        return blob
    return normalized(text) if _LOCAL_TIME.search(text) else blob
