"""WebSocket conversations with either side, for the quote-feed scenarios.

The parity harness compares HTTP calls; the WebSocket feed is checked by holding the same
conversation with each side and comparing the transcripts (the real one is kept for the fake
run of the same scenario). The real side is reached only in ``--target=real|parity`` runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2
from httpx2.websockets import WebSocketDisconnect as RealDisconnect
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect as FakeDisconnect

if TYPE_CHECKING:
    from fauxcall.testing import FakeUnderTest

LIVE_WITHIN_S = 60.0
KEY = "readonly-key"  # the documented read-only key: the feed only reads
TRANSCRIPTS: dict[str, list[Any]] = {}
"""Real transcripts by scenario, compared with the fake's (real runs first in parity mode)."""


@dataclass(slots=True)
class Conversation:
    """What one side answered: JSON messages, then the close (code, reason) if it closed."""

    messages: list[Any] = field(default_factory=list)
    closed: tuple[int, str] | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


async def talk(
    fake: FakeUnderTest, *, real: bool, sends: list[str], expect: int, key: str | None = KEY
) -> Conversation:
    """Send ``sends`` (text frames) and read ``expect`` messages or until the socket closes."""
    query = "" if key is None else f"?api_key={key}"
    if real:
        return await _real(fake, f"/v1/ws/quotes{query}", sends, expect)
    return _fake(fake, f"/v1/ws/quotes{query}", sends, expect)


async def _real(fake: FakeUnderTest, path: str, sends: list[str], expect: int) -> Conversation:
    base = (fake.real_base_url or "").replace("http://", "ws://").replace("https://", "wss://")
    talk = Conversation()
    async with httpx2.AsyncClient() as http, http.websocket(base + path, timeout=15) as ws:
        for text in sends:
            await ws.send_text(text)
        try:
            while len(talk.messages) < expect:
                talk.messages.append(json.loads(await ws.receive_text(timeout=10)))
            if expect == 0 or talk.messages[-1] == {"type": "done"}:
                await ws.receive_text(timeout=3)
        except RealDisconnect as closed:
            talk.closed = (closed.code, closed.reason or "")
        except TimeoutError:
            pass
    return talk


def _fake(fake: FakeUnderTest, path: str, sends: list[str], expect: int) -> Conversation:
    talk = Conversation()
    with TestClient(fake.app_factory()) as http, http.websocket_connect(path) as ws:
        for text in sends:
            ws.send_text(text)
        try:
            while len(talk.messages) < expect:
                talk.messages.append(json.loads(ws.receive_text()))
            if expect == 0 or talk.messages[-1] == {"type": "done"}:
                ws.receive_text()
        except FakeDisconnect as closed:
            talk.closed = (closed.code, closed.reason or "")
    return talk


def _as_of(value: str, started_at: datetime) -> str:
    """A quote's as_of as behavior: 'live' while it tracks the clock (open market: 1.5 s before
    each round), else the instant itself (a closed market's last close, equal on both sides)."""
    lag = (started_at - datetime.fromisoformat(value)).total_seconds()
    return "live" if abs(lag) < LIVE_WITHIN_S else value


def shape(talk: Conversation) -> list[Any]:
    """A transcript without live values: types, symbols, prices, and as_of as behavior."""
    kept: list[Any] = [
        {
            key: _as_of(value, talk.started_at) if key == "as_of" else value
            for key, value in message.items()
        }
        for message in talk.messages
    ]
    return [*kept, talk.closed]
