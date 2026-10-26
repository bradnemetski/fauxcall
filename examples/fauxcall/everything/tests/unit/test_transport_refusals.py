"""The HTTP refusals introspection recorded, replayed against the fake: same status, same body.

Raw POSTs, DELETEs and PUTs can't run in parity (the approval covers tool calls), so the
recordings are the evidence they are checked against. A recorded session id (redacted) becomes
a live session on the fake, except where the real server refused the id as unknown.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.recording.store import RecordingStore
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fauxcall.model.recording import Exchange

PROBES = (
    "000006",
    "000014",
    "000015",
    "000016",
    "000017",
    "000018",
    "000019",
    "000020",
    "000021",
    "000022",
    "000024",
    "000025",
    "000026",
    "000027",
    "000028",
    "000030",
    "000031",
    "000032",
    "000033",
    "000034",
    "000035",
    "000036",
    "000038",
)
UNKNOWN = "No valid session ID provided"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
REBUILT: dict[str, tuple[str, bytes | None]] = {
    "000025": ("/fauxcall-probe-unknown-route", None),
    "000031": ("/mcp", b'{"hello": "world"}'),
    "000036": ("/mcp", json.dumps([{**PING, "id": n} for n in range(101)]).encode()),
    "000038": ("/mcp", b'{"pad": "' + b"x" * (4 * 1024 * 1024) + b'"}'),
}
"""Probes whose recordings keep neither the request body nor (000025) the probed path."""
STALE = "11111111-2222-4333-8444-555555555555"


def _probes() -> list[Exchange]:
    store = RecordingStore(Settings().data_dir / "recordings")
    return [exchange for _, exchange in store.iter_exchanges() if exchange.id in PROBES]


def _body(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return text


@pytest.mark.parametrize("exchange", _probes(), ids=lambda e: f"{e.id}-{e.endpoint}")
async def test_recorded_refusal(exchange: Exchange) -> None:
    recorded = exchange.response
    assert recorded is not None
    body = exchange.request.body
    content: bytes | None = None
    if body.kind == "json":
        content = json.dumps(body.json_value).encode()
    elif body.text is not None:
        content = body.text.encode()
    path = exchange.request.path
    if exchange.id in REBUILT:
        path, content = REBUILT[exchange.id]
    expected_text = recorded.body.text if recorded.body.text is not None else ""
    if recorded.body.kind == "json":
        expected_text = json.dumps(recorded.body.json_value)
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = []
        for name, value in exchange.request.headers:
            if name.lower() in {"host", "content-length", "connection", "accept-encoding"}:
                continue
            if name.lower() == "mcp-session-id":
                value = STALE if UNKNOWN in expected_text else str(wire.session_id)  # noqa: PLW2901
            headers.append((name, value))
        if exchange.endpoint == "MCP GET:second_stream":
            await wire.listen()  # the first stream, which the probe's GET comes second to
        async with (
            asyncio.timeout(10),
            http.stream(
                exchange.request.method.value, path, headers=headers, content=content
            ) as response,
        ):
            status = response.status_code
            streaming = response.headers.get("content-type", "").startswith("text/event-stream")
            text = "" if streaming else (await response.aread()).decode()
        await wire.close()
    assert status == recorded.status
    if not streaming:
        assert _body(text) == _body(expected_text)
