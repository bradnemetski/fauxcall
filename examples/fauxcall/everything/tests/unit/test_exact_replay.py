"""Recorded answers, replayed against the fake at the moment they were recorded: values must match.

``test_recorded.py`` checks status and shape; this checks every value of every deterministic
answer the real server gave (texts, numbers, errors, the times inside dynamic resources), so the
handlers are held to the recordings, not only to their structure.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from fauxcall import jsonfmt
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import at_recording_time, settings_for_tests

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fauxcall.model.recording import Exchange
    from fauxcall.types import JsonValue

STATELESS_TOOLS = frozenset(
    {
        "echo",
        "get-annotated-message",
        "get-env",
        "get-resource-links",
        "get-resource-reference",
        "get-structured-content",
        "get-sum",
        "get-tiny-image",
        "trigger-long-running-operation",
        "toggle-simulated-logging",
        "toggle-subscriber-updates",
    }
)
METHODS = (
    "MCP resources/read",
    "MCP prompts/get",
    "MCP completion/complete",
    "MCP logging/setLevel",
    "MCP ping",
    "MCP resources/templates/list",
    "MCP prompts/list",
    "MCP foo/bar",
)
SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _replayable(exchange: Exchange) -> bool:
    if exchange.response is None or exchange.purpose == "discovery":
        return False
    sent = exchange.request.body.parsed()
    params = sent.get("params") if isinstance(sent, dict) else None
    if exchange.endpoint.startswith("MCP tools/call:"):
        tool = exchange.endpoint.split(":", 1)[1]
        if tool.startswith("toggle-") and "Stopped" in jsonfmt.dumps(
            exchange.response.body.parsed()
        ):
            return False  # the second toggle of a session: covered by test_session_features.py
        return tool in STATELESS_TOOLS or (
            tool == "gzip-file-as-resource" and exchange.purpose != "write_readback"
        )
    if exchange.endpoint.startswith("MCP resources/read"):
        uri = params.get("uri") if isinstance(params, dict) else None
        reply = exchange.response.body.parsed()
        made_by_gzip = isinstance(uri, str) and uri.startswith("demo://resource/session/")
        return not (made_by_gzip and isinstance(reply, dict) and "result" in reply)
    return exchange.endpoint.startswith(METHODS)


def _cases() -> list[Exchange]:
    store = RecordingStore(Settings().data_dir / "recordings")
    return [exchange for _, exchange in store.iter_exchanges() if _replayable(exchange)]


CASES = _cases()


@pytest.fixture
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


def _normalized(value: JsonValue, session: str | None) -> JsonValue:
    """The answer with this replay's session id written as the recorded one's placeholder."""
    text = jsonfmt.dumps(value)
    if session:
        text = text.replace(session, "<session>")
    return jsonfmt.plain(jsonfmt.loads(SESSION.sub("<session>", text)))


async def _replay(app: FastAPI, exchange: Exchange) -> tuple[JsonValue, JsonValue]:
    sent = exchange.request.body.parsed()
    assert isinstance(sent, dict)
    recorded = exchange.response.body.parsed() if exchange.response else None
    assert isinstance(recorded, dict)
    async with streaming_client(app) as client:
        wire = McpWireClient("http://fake.test/mcp", client)
        try:
            await wire.initialize()
            call = await wire.call(str(sent["method"]), sent.get("params"))
        finally:
            await wire.close()
    actual = call.response if isinstance(call.response, dict) else {}
    member = "result" if "result" in recorded else "error"
    return (
        _normalized(recorded.get(member), None),
        _normalized(actual.get(member), wire.session_id),
    )


@pytest.mark.parametrize("exchange", CASES, ids=[f"{e.id}-{e.endpoint}" for e in CASES])
async def test_recorded_values(app: FastAPI, exchange: Exchange) -> None:
    runtime_of_app(app).reset()
    with at_recording_time(app, exchange):
        expected, actual = await _replay(app, exchange)
    assert actual == expected
