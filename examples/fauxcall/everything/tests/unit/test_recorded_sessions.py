"""Every recorded plan, replayed against the fake in one session, the way it was recorded.

Each plan ran in one MCP session declaring the plan's client capabilities, the recorder
answering the server's requests (sampling, elicitation, roots, tasks) with each call's
``client_replies``. Replayed the same way and on the recorded timeline (between two calls the
fake's clock moves as far as the real one did), the fake must answer every call as the real
server did: same results and errors, member for member, once what differs run to run is set
aside (session, task and elicitation ids, timestamps, the local time stamped on dynamic
resources). A call recorded outside the session (``auth: none``: a handshake of its own) is
replayed outside it too.

Plans that listened to the client's GET stream also compare what the server pushed there: every
message sent because something happened (a task's status changed, the resource list changed,
roots were asked for, a subscription was logged), in order. The periodic ones (simulated
logging and resource updates every 5 s, on wall-clock time) are left to ``test_sessions.py``.

Task polls (``tasks/get``, ``tasks/list``) are compared by shape: a poll a few milliseconds from
a stage boundary may see either side of it. ``[REDACTED]`` in a recording (an over-cautious
redaction of ``maxTokens``' description) matches anything.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Final

import pytest
from fauxcall import jsonfmt
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.model.plan import RecordingPlan
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime.app import runtime_of_app
from fauxcall.storage import read_model
from fauxcall.testing.compare import shape_differences
from fauxcall.testing.support import settings_for_tests
from mcp_session import connect, normalized

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fauxcall.clock import ControlledClock
    from fauxcall.model.plan import PlannedCall
    from fauxcall.model.recording import Exchange
    from fauxcall.types import JsonObject, JsonValue

DATA: Final = Settings().data_dir
PLANS: Final = sorted((DATA / "plans").glob("*.json"))
SHAPE_ONLY: Final = frozenset({"tasks/get", "tasks/list"})
REDACTED: Final = "[REDACTED]"
KNOWN_GAPS: Final = {
    "ps-unknown-tool-null-args": "FauxCall's runtime refuses an unknown tool before its arguments",
    "ps-name-number": "FauxCall's runtime refuses any name that isn't a string as a missing one",
}
"""Calls FauxCall's runtime answers before the fake's handlers can (README: known differences).

They must still differ: once the runtime answers them as recorded, drop them from here.
"""
STREAM_WAIT_SECONDS: Final = 5.0


def _recorded() -> tuple[dict[str, list[Exchange]], list[Exchange]]:
    """Plan calls -> their exchanges, and every recorded GET stream."""
    by_call: dict[str, list[Exchange]] = defaultdict(list)
    streams: list[Exchange] = []
    for _, exchange in RecordingStore(DATA / "recordings").iter_exchanges():
        if exchange.plan_call:
            by_call[exchange.plan_call].append(exchange)
        elif exchange.endpoint == "MCP GET:stream":
            streams.append(exchange)
    return by_call, streams


RECORDED, STREAMS = _recorded()


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


def _swap(value: JsonValue, ids: dict[str, str]) -> JsonValue:
    """The recorded params, with the real server's task ids replaced by the fake's."""
    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, list):
        return [_swap(item, ids) for item in value]
    if isinstance(value, dict):
        return {key: _swap(item, ids) for key, item in value.items()}
    return value


def _differences(recorded: JsonValue, fake: JsonValue, path: str = "$") -> list[str]:
    """Where the fake's normalized answer differs from the recorded one."""
    if recorded == REDACTED:
        return []
    if isinstance(recorded, dict) and isinstance(fake, dict):
        if list(recorded) != list(fake):
            return [f"{path}: members {list(recorded)} vs {list(fake)}"]
        return [
            d for key in recorded for d in _differences(recorded[key], fake[key], f"{path}.{key}")
        ]
    if isinstance(recorded, list) and isinstance(fake, list):
        if len(recorded) != len(fake):
            return [f"{path}: {len(recorded)} items vs {len(fake)}"]
        return [
            d
            for i, (r, f) in enumerate(zip(recorded, fake, strict=True))
            for d in _differences(r, f, f"{path}[{i}]")
        ]
    return [] if recorded == fake else [f"{path}: {recorded!r} vs {fake!r}"]


def _payload(message: JsonValue) -> JsonValue:
    reply = message if isinstance(message, dict) else {}
    return reply.get("result", reply.get("error"))


def _learn_ids(recorded: JsonValue, fake: JsonValue, ids: dict[str, str]) -> None:
    """Map the real task id of a created task to the fake's."""
    real_task = recorded.get("task") if isinstance(recorded, dict) else None
    fake_task = fake.get("task") if isinstance(fake, dict) else None
    if isinstance(real_task, dict) and isinstance(fake_task, dict):
        ids[str(real_task.get("taskId"))] = str(fake_task.get("taskId"))


def _event_driven(message: JsonValue) -> bool:
    """Whether a GET-stream message answers something that happened (not a periodic one)."""
    if not isinstance(message, dict):
        return False
    method = message.get("method")
    if method == "notifications/resources/updated":  # every 5 s while subscribed
        return False
    if method == "notifications/message":  # simulated logging is periodic; these are not
        params = message.get("params")
        data = params.get("data") if isinstance(params, dict) else None
        return isinstance(params, dict) and ("logger" in params or str(data).startswith("Received"))
    return True


def _pushed(messages: list[JsonValue]) -> list[JsonValue]:
    """The event-driven GET-stream messages, normalized, by source, each source's in order.

    Every task's status changes are one source (in the order the tasks appeared), everything else
    another: concurrent tasks interleave by the wall clock, which a replay in fast mode compresses.
    A server request's id is set aside (the number of requests sent before it differs).
    """
    sources: dict[str, list[JsonValue]] = {"": []}
    for message in messages:
        if not (_event_driven(message) and isinstance(message, dict)):
            continue
        params = message.get("params")
        task = params.get("taskId") if isinstance(params, dict) else None
        key = str(task) if message.get("method") == "notifications/tasks/status" else ""
        found = normalized({name: value for name, value in message.items() if name != "id"})
        sources.setdefault(key, []).append(found)
    by_source: list[JsonValue] = [*sources.values()]
    return by_source


def _count(by_source: list[JsonValue]) -> int:
    return sum(len(source) for source in by_source if isinstance(source, list))


def _stream_of(plan: RecordingPlan) -> Exchange | None:
    """The GET stream recorded for a listening plan: the last one opened before its first call."""
    starts = [e.started_ns for call in plan.calls for e in RECORDED.get(call.id, [])]
    if not plan.listen or not starts:
        return None
    before = [s for s in STREAMS if s.started_ns <= min(starts)]
    return max(before, key=lambda s: s.started_ns, default=None)


def _messages_in(data: str | None) -> list[JsonValue]:
    """The JSON-RPC message an SSE event carries (none for a priming or keep-alive event)."""
    try:
        return [jsonfmt.loads(data)] if data else []
    except ValueError:
        return []


def _recorded_messages(stream: Exchange) -> list[JsonValue]:
    return [m for event in stream.stream or [] for m in _messages_in(event.data)]


class Timeline:
    """Moves the fake's clock along the recording: as far between calls as the real one went."""

    def __init__(self, clock: ControlledClock, first_ns: int) -> None:
        self.clock = clock
        self.real_start = first_ns
        self.fake_start = clock.now_ns()

    def reach(self, real_ns: int) -> None:
        behind = self.fake_start + (real_ns - self.real_start) - self.clock.now_ns()
        if behind > 0:
            self.clock.advance(behind / 1e9)


async def _outside_session(wire: McpWireClient, method: str, params: JsonValue) -> JsonValue:
    """A request on its own, without the session (``auth: none`` in the plan)."""
    alone = McpWireClient(wire.url, wire.http)
    call = await alone.post(protocol.request(1, method, params), include_session=False)
    return _payload(call.response)


async def _compared(
    wire: McpWireClient, call: PlannedCall, exchange: Exchange, ids: dict[str, str]
) -> list[str]:
    """Replay one recorded exchange; how the fake's answer differs from the recorded one."""
    sent = exchange.request.body.parsed()
    assert isinstance(sent, dict)
    method = str(sent["method"])
    params = _swap(sent.get("params"), ids)
    if call.request.auth == "none":
        fake = await _outside_session(wire, method, params)
    else:
        fake = _payload((await wire.call(method, params)).response)
    assert exchange.response is not None
    recorded = _payload(exchange.response.body.parsed())
    _learn_ids(recorded, fake, ids)
    if method in SHAPE_ONLY:
        return shape_differences(recorded, fake)
    return _differences(normalized(recorded), normalized(fake))


async def _settled_stream(wire: McpWireClient, wanted: int) -> list[JsonValue]:
    """What the fake pushed, once its event-driven messages are in (or a few seconds passed)."""
    deadline = time.monotonic() + STREAM_WAIT_SECONDS
    listening = wire._listening  # what arrived so far, before the stream is closed
    while listening is not None and time.monotonic() < deadline:
        arrived = [m for _, _, data, _ in listening.events for m in _messages_in(data)]
        if _count(_pushed(arrived)) >= wanted:
            break
        await asyncio.sleep(0.05)
    stopped = await wire.stop_listening()
    return stopped.messages if stopped is not None else []


@pytest.mark.parametrize("plan_path", PLANS, ids=[p.stem for p in PLANS])
async def test_recorded_plan_session(app: FastAPI, plan_path: Path) -> None:
    runtime = runtime_of_app(app)
    runtime.reset(journal=True)
    plan = read_model(plan_path, RecordingPlan)
    capabilities: JsonObject | None = (
        plan.client_capabilities if isinstance(plan.client_capabilities, dict) else None
    )
    exchanges = [e for call in plan.calls for e in RECORDED.get(call.id, [])]
    assert exchanges, f"no recordings for plan {plan.name}"
    stream = _stream_of(plan)
    timeline = Timeline(runtime.clocks.default, min(e.started_ns for e in exchanges))
    if stream is not None:
        timeline.real_start = stream.started_ns  # the session opened its stream first
    ids: dict[str, str] = {}
    problems: list[str] = []
    gaps_closed: list[str] = []
    async with connect(app, capabilities=capabilities, listen=plan.listen) as wire:
        for call in plan.calls:
            wire.on_request = replier(call.client_replies)
            for exchange in RECORDED.get(call.id, []):
                timeline.reach(exchange.started_ns)
                found = await _compared(wire, call, exchange, ids)
                if call.id in KNOWN_GAPS:
                    gaps_closed += [] if found else [call.id]
                    continue
                problems += [f"{call.id} ({exchange.id}): {d}" for d in found[:5]]
        if stream is not None:
            timeline.reach(stream.started_ns + int(stream.elapsed_ms * 1e6))
            recorded = _pushed(_recorded_messages(stream))
            fake = _pushed(await _settled_stream(wire, _count(recorded)))
            found = _differences(recorded, fake)
            problems += [f"GET stream ({stream.id}): {d}" for d in found[:5]]
    assert not gaps_closed, f"answered as recorded now, drop from KNOWN_GAPS: {gaps_closed}"
    assert not problems, "\n".join(problems)
