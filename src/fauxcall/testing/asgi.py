"""An in-process ASGI transport that streams responses (for SSE and other endless bodies).

``httpx2.ASGITransport`` buffers a whole response before returning it, which never finishes for an
endless stream: an MCP ``GET`` stream or the legacy HTTP+SSE transport.
:class:`StreamingASGITransport` streams bodies chunk by chunk instead and, like a real server, tells
the app when the client hangs up. Use :func:`streaming_client` to talk to a fake's app in-process.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import httpx2

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, MutableMapping

    from starlette.types import ASGIApp

    Message = MutableMapping[str, Any]


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

    def __init__(self, app: ASGIApp, *, close_timeout: float = 5.0) -> None:
        self.app = app
        self.close_timeout = close_timeout

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Run the app for ``request`` and return as soon as the response starts."""
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


def streaming_client(app: ASGIApp, base_url: str = "http://fake.test") -> httpx2.AsyncClient:
    """An in-process client for ``app`` that streams responses."""
    return httpx2.AsyncClient(transport=StreamingASGITransport(app), base_url=base_url)
