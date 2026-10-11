"""Streaming replay: Server-Sent Events, chunked responses and WebSockets.

Recorded events are replayed with their original spacing (scaled by ``speed``); JSON payloads go
through the endpoint's body policies so timestamps inside events are "now" when each event is sent.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import TYPE_CHECKING, Final

from starlette.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from fauxcall import jsonfmt
from fauxcall.logs import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable, Sequence

    from starlette.responses import Response

    from fauxcall.model.recording import Exchange, StreamEvent
    from fauxcall.runtime.context import RequestContext

log = get_logger(__name__)

_SERVER_KINDS: Final = frozenset({"ws_recv", "ws_close"})


def _render_data(ctx: RequestContext, data: str | None) -> str | None:
    """Apply body policies to a JSON payload (non-JSON passes through unchanged)."""
    if data is None:
        return None
    try:
        value = jsonfmt.loads(data)
    except (ValueError, json.JSONDecodeError):
        return data
    rendered = ctx.render_body(value)
    return jsonfmt.dumps(rendered, ctx.model.json_style)


def sse_frame(event: StreamEvent, data: str | None) -> str:
    """One SSE frame (``id``/``event``/``retry``/``data`` lines, blank-line terminated)."""
    lines: list[str] = []
    if event.id is not None:
        lines.append(f"id: {event.id}")
    if event.event is not None and event.event != "message":
        lines.append(f"event: {event.event}")
    if event.retry is not None:
        lines.append(f"retry: {event.retry}")
    lines.extend(f"data: {line}" for line in (data or "").split("\n"))
    return "\n".join(lines) + "\n\n"


async def _paced(
    events: Sequence[StreamEvent], speed: float, sleep: Callable[[float], object]
) -> AsyncIterator[StreamEvent]:
    previous = 0.0
    for event in events:
        delay = max(0.0, (event.t_ms - previous) / 1000.0 / speed)
        previous = event.t_ms
        if delay:
            await sleep(delay)  # type: ignore[misc]
        yield event


def sse_response(
    ctx: RequestContext,
    exchange: Exchange,
    *,
    speed: float = 1.0,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> Response:
    """Replay a recorded SSE stream."""
    events = [event for event in exchange.stream if event.kind == "sse"]
    response = exchange.response
    content_type = (response.header("content-type") if response else None) or "text/event-stream"

    async def body() -> AsyncIterator[bytes]:
        async for event in _paced(events, speed, sleep):
            yield sse_frame(event, _render_data(ctx, event.data)).encode()

    headers = {"cache-control": "no-cache", **ctx.extra_headers}
    return StreamingResponse(body(), status_code=200, media_type=content_type, headers=headers)


def chunked_response(
    ctx: RequestContext,
    exchange: Exchange,
    *,
    speed: float = 1.0,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> Response:
    """Replay a recorded chunked (non-SSE) stream."""
    events = [event for event in exchange.stream if event.kind == "chunk"]
    response = exchange.response
    content_type = response.header("content-type") if response else None

    async def body() -> AsyncIterator[bytes]:
        async for event in _paced(events, speed, sleep):
            yield (_render_data(ctx, event.data) or "").encode()

    return StreamingResponse(
        body(), status_code=response.status if response else 200, media_type=content_type
    )


def _same_message(sent: str, expected: str | None) -> bool:
    if expected is None:
        return False
    try:
        return jsonfmt.plain(jsonfmt.loads(sent)) == jsonfmt.plain(jsonfmt.loads(expected))
    except (ValueError, json.JSONDecodeError):
        return sent == expected


async def replay_websocket(
    websocket: WebSocket,
    ctx: RequestContext,
    exchanges: Sequence[Exchange],
    *,
    respond_to: str = "match",
    speed: float = 1.0,
    loop: bool = False,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> None:
    """Replay a recorded WebSocket conversation.

    Server messages recorded before the client said anything (greetings, connection acks) are sent
    on connect. After that, the server messages that followed a client message in the recording
    are sent when the client sends a matching message (``respond_to="match"``). ``"none"`` replays
    the server side unconditionally on its own timeline (repeatedly with ``loop``); ``"echo"``
    echoes client messages. Recorded spacing is kept, scaled by ``speed``.
    """
    await websocket.accept()
    if not exchanges:
        await websocket.close(code=1011)
        return
    script = list(exchanges[0].stream)
    try:
        if respond_to == "echo":
            while True:
                await websocket.send_text(await websocket.receive_text())
        if respond_to == "none":
            pushed = [event for event in script if event.kind in _SERVER_KINDS]
            closed = await _play(websocket, ctx, pushed, 0.0, speed, sleep)
            while loop and pushed and not closed:
                await sleep(_loop_pause(pushed, speed))  # type: ignore[misc]
                closed = await _play(websocket, ctx, pushed, 0.0, speed, sleep)
            if not closed:
                await websocket.close(code=1000)
            return
        await _conversation(websocket, ctx, exchanges, speed, sleep)
    except WebSocketDisconnect:
        log.debug("websocket_client_disconnected", endpoint=ctx.endpoint_id)


async def _play(
    websocket: WebSocket,
    ctx: RequestContext,
    events: Sequence[StreamEvent],
    start_ms: float,
    speed: float,
    sleep: Callable[[float], object],
) -> bool:
    """Send recorded server messages with their spacing from ``start_ms``; True once closed."""
    previous = start_ms
    for event in events:
        delay = max(0.0, (event.t_ms - previous) / 1000.0 / speed)
        previous = max(previous, event.t_ms)
        if delay:
            await sleep(delay)  # type: ignore[misc]
        if event.kind == "ws_close":
            await websocket.close(code=event.code or 1000)
            return True
        await websocket.send_text(_render_data(ctx, event.data) or "")
    return False


def _loop_pause(events: Sequence[StreamEvent], speed: float) -> float:
    """Seconds between repeats of a looped push stream: the recording's average message gap."""
    sent = [event.t_ms for event in events]
    gap_ms = (sent[-1] - sent[0]) / (len(sent) - 1) if len(sent) > 1 else 0.0
    return max(gap_ms, 1000.0) / 1000.0 / speed


async def _conversation(
    websocket: WebSocket,
    ctx: RequestContext,
    exchanges: Sequence[Exchange],
    speed: float,
    sleep: Callable[[float], object],
) -> None:
    scripts = [list(exchange.stream) for exchange in exchanges]
    if await _play(websocket, ctx, _leading(scripts[0]), 0.0, speed, sleep):
        return
    while True:
        sent = await websocket.receive_text()
        found = _find_reply(scripts, sent)
        if found is None:
            log.info("websocket_unmatched_message", endpoint=ctx.endpoint_id, message=sent[:200])
            continue
        sent_at, reply = found
        if await _play(websocket, ctx, reply, sent_at, speed, sleep):
            return


def _server_side(events: Iterable[StreamEvent]) -> list[StreamEvent]:
    """What the server sent (messages and a close) before the client's next message."""
    before_send = itertools.takewhile(lambda event: event.kind != "ws_send", events)
    return [event for event in before_send if event.kind in _SERVER_KINDS]


def _leading(script: Sequence[StreamEvent]) -> list[StreamEvent]:
    """Server messages recorded before the client's first message."""
    return _server_side(script)


def _find_reply(
    scripts: Sequence[list[StreamEvent]], sent: str
) -> tuple[float, list[StreamEvent]] | None:
    """The recorded client message equal to ``sent``: when it was sent and the server's answer.

    The reply is every server message (and a close) up to the client's next message.
    """
    for script in scripts:
        for index, event in enumerate(script):
            if event.kind == "ws_send" and _same_message(sent, event.data):
                return event.t_ms, _server_side(script[index + 1 :])
    return None


def pick_stream_exchange(exchanges: Sequence[Exchange], query: dict[str, str]) -> Exchange | None:
    """The recorded stream whose query best matches (exact, else most shared params, else first)."""
    if not exchanges:
        return None

    def overlap(exchange: Exchange) -> int:
        recorded = dict(exchange.request.query)
        return sum(1 for key, value in query.items() if recorded.get(key) == value)

    return max(exchanges, key=overlap)
