"""Every recorded MCP session, replayed in order against the fake, gets the recorded values.

``test_recorded.py`` checks status and shape call by call; this replays each recording plan's
session as it ran (one MCP session, same order, the fake's clock pinned to the moment each call
was recorded), so state carries over (gzip then read, toggles, task lifecycles) and results,
errors and progress notifications must match value for value. Ids the server makes up (task ids,
the MCP session id) are mapped; task timestamps may differ by a few milliseconds.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Final

import pytest
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fauxcall.model.recording import Exchange
    from fauxcall.types import JsonValue

DEFAULT_SESSION: Final = "default"
ISO_MS: Final = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
SESSION_ID: Final = re.compile(r"session [0-9a-f-]{32,36}")
TIMESTAMP_TOLERANCE_S: Final = 0.025

KNOWN_GAPS: Final = {
    "init-unknown": "FauxCall answers a second initialize in a session instead of refusing it",
    "gz-list": "FauxCall serves resources/list from the model: session resources are missing",
    "tl-page1": "11 tasks running at once: the real server reached one stage boundary ~1 s late",
    "list-after": "FauxCall serves resources/list from the model: session resources are missing",
    "list-end": "FauxCall serves resources/list from the model: session resources are missing",
    "call-no-name": "FauxCall answers tools/call without a name as an unknown tool, not -32603",
    "call-name-num": "FauxCall answers tools/call with a non-string name as an unknown tool",
}
"""Recorded calls the fake cannot answer identically, and why (see the model's notes)."""


def _sessions() -> list[list[Exchange]]:
    """Each recording plan's exchanges, in order (a plan run is one MCP session)."""
    store = RecordingStore(Settings().data_dir / "recordings")
    exchanges = sorted((e for _, e in store.iter_exchanges()), key=lambda e: e.id)
    sessions: list[list[Exchange]] = []
    for exchange in exchanges:
        if exchange.purpose == "discovery":
            continue
        if exchange.endpoint == "MCP initialize":
            sessions.append([])
        elif sessions and exchange.response is not None:
            sessions[-1].append(exchange)
    return [session for session in sessions if session]


SESSIONS: Final = _sessions()


def _rpc(exchange: Exchange) -> tuple[str, JsonValue]:
    sent = exchange.request.body.parsed()
    assert isinstance(sent, dict)
    return str(sent["method"]), sent.get("params")


def _replace(value: JsonValue, mapping: dict[str, str]) -> JsonValue:
    """``value`` with every string mapped (task ids) and session ids blanked."""
    if isinstance(value, dict):
        return {key: _replace(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, mapping) for item in value]
    if isinstance(value, str):
        text = mapping.get(value, value)
        for old, new in mapping.items():
            text = text.replace(old, new)
        return SESSION_ID.sub("session <id>", text)
    return value


def _same_moment(recorded: str, actual: str) -> bool:
    """Two ``toISOString`` times within the tolerance (the fake's clock is pinned per call)."""
    gap = datetime.fromisoformat(recorded).timestamp() - datetime.fromisoformat(actual).timestamp()
    return abs(gap) <= TIMESTAMP_TOLERANCE_S


def _differences(recorded: JsonValue, actual: JsonValue, path: str = "$") -> list[str]:
    if isinstance(recorded, dict) and isinstance(actual, dict):
        if list(recorded) != list(actual):
            return [f"{path}: keys {list(recorded)} vs {list(actual)}"]
        pairs = [(recorded[key], actual[key], f"{path}.{key}") for key in recorded]
    elif isinstance(recorded, list) and isinstance(actual, list):
        if len(recorded) != len(actual):
            return [f"{path}: {len(recorded)} items vs {len(actual)}"]
        pairs = [
            (r, a, f"{path}[{i}]") for i, (r, a) in enumerate(zip(recorded, actual, strict=True))
        ]
    elif isinstance(recorded, str) and isinstance(actual, str) and ISO_MS.fullmatch(recorded):
        same = ISO_MS.fullmatch(actual) is not None and _same_moment(recorded, actual)
        return [] if same else [f"{path}: {recorded} vs {actual}"]
    elif recorded != actual or type(recorded) is not type(actual):
        return [f"{path}: {json.dumps(recorded)[:160]} vs {json.dumps(actual)[:160]}"]
    else:
        pairs = []
    return [d for r, a, where in pairs for d in _differences(r, a, where)]


def _task_id(reply: JsonValue) -> str | None:
    """The id of the task a ``tools/call`` created, if it created one."""
    result = reply.get("result") if isinstance(reply, dict) else None
    task = result.get("task") if isinstance(result, dict) else None
    return str(task["taskId"]) if isinstance(task, dict) else None


def _notifications(events: list[JsonValue]) -> list[JsonValue]:
    """Notifications sent before the answer; FauxCall frames them ``jsonrpc`` first (the real
    server last), which no client sees, so their members are compared in a fixed order."""
    found: list[JsonValue] = []
    for data in events:
        message = json.loads(data) if isinstance(data, str) else data
        if isinstance(message, dict) and "method" in message:
            found.append({key: message[key] for key in sorted(message)})
    return found


@pytest.mark.parametrize("session", SESSIONS, ids=[s[0].id for s in SESSIONS])
async def test_recorded_session_values(session: list[Exchange]) -> None:
    app = build_app(settings_for_tests(Settings))
    runtime = runtime_of_app(app)
    ids: dict[str, str] = {}  # the fake's task ids -> the recorded ones
    to_fake: dict[str, str] = {}  # and back, for the calls that name them
    problems: list[str] = []
    gaps: set[str] = set()
    async with streaming_client(app) as client:
        wire = McpWireClient(str(client.base_url).rstrip("/") + "/mcp", client)
        await wire.initialize()
        for exchange in session:
            assert exchange.response is not None
            method, params = _rpc(exchange)
            moment = exchange.started_ns + int(exchange.elapsed_ms * 500_000)
            if exchange.endpoint.startswith("MCP tasks/result") and exchange.elapsed_ms > 100:
                # it waited for the task: the fast fake answers at once, at the moment it ended
                moment = exchange.started_ns + int(exchange.elapsed_ms * 1_000_000)
            runtime.clocks.override(DEFAULT_SESSION).freeze(moment)
            call = await wire.call(method, _replace(params, to_fake))
            recorded = exchange.response.body.parsed()
            assert isinstance(recorded, dict)
            actual = call.response if isinstance(call.response, dict) else {}
            fake_id, real_id = _task_id(actual), _task_id(recorded)
            if fake_id is not None and real_id is not None:
                ids[fake_id], to_fake[real_id] = real_id, fake_id
            mapped = _replace(actual, ids)
            assert isinstance(mapped, dict)
            expected = {k: v for k, v in recorded.items() if k in {"result", "error"}}
            got = {k: v for k, v in mapped.items() if k in {"result", "error"}}
            found = _differences(_replace(expected, {}), got)
            found += _differences(
                _replace(_notifications([e.data for e in exchange.stream]), {}),
                _replace(_notifications([e[2] for e in call.sse_events]), ids),
                "notifications",
            )
            label = exchange.plan_call or exchange.id
            if label in KNOWN_GAPS and not found:
                problems.append(f"{exchange.id} {label}: matches now, drop it from KNOWN_GAPS")
            elif found and label in KNOWN_GAPS:
                gaps.add(label)
            elif found:
                problems.append(
                    f"{exchange.id} {label} {exchange.endpoint}: " + "; ".join(found[:3])
                )
        await wire.close()
    assert not problems, "\n".join(problems)
