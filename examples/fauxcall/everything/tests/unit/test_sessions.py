"""Whole recorded sessions replayed call by call against the fake, with the client's answers.

Each recording plan ran in one MCP session: the client declared the plan's capabilities, held its
GET stream open when the plan listened, and answered the server's requests (sampling,
elicitation, roots, task polls) with each call's ``client_replies``. Replaying the same session
against the fake must give the recorded answers, values included: what one call changes (a URL
elicitation's one-shot error, cached roots, gzip resources, logging state) shows in the next.
What the server sends on the call's stream before answering (requests to the client, progress)
is compared too. Each call runs on the fake clock of the moment it was recorded (the times inside
dynamic resources match to the second); server-made ids (session ids, random elicitation and
task ids)
and task timestamps, which carry milliseconds, are compared by shape.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from fauxcall import jsonfmt
from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.model.plan import RecordingPlan
from fauxcall.recording.store import RecordingStore
from fauxcall.storage import read_model
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import at_recording_time, settings_for_tests

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fauxcall.model.recording import Exchange
    from fauxcall.types import JsonValue

PLANS = (
    "access",
    "sampling",
    "sampling-rerecord",
    "elicitation",
    "url-elicitation",
    "roots-two",
    "roots-empty",
    "roots-error",
    "roots-no-stream",
    "async",
    "capability-gating",
    "capability-sampling-only-list",
    "capability-tasks-sampling",
    "capability-tasks-elicitation",
    "capability-all",
    "protocol",
    "gzip",
    "echo",
    "get-sum",
    "resources",
    "prompts",
    "completions",
    "review-roots-no-member",
    "review-async-malformed",
    "review-schema-refusals",
    "trigger-long-running-operation",
)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TASK_ID = re.compile(r"\b[0-9a-f]{32}\b")
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
MAYBE_TIMED = ("access",)
"""Plans whose answers hold the times they were made (compared by their spelling)."""


def _recorded(store: RecordingStore, plan: RecordingPlan) -> dict[str, list[Exchange]]:
    """The latest recorded run of each call of the plan (repeats in order)."""
    ids = {call.id for call in plan.calls}
    found: dict[str, list[Exchange]] = {}
    for _, exchange in store.iter_exchanges():
        call = exchange.plan_call
        if call in ids and exchange.endpoint == next(
            c.endpoint for c in plan.calls if c.id == call
        ):
            found.setdefault(str(call), []).append(exchange)
    repeats = {call.id: call.repeat for call in plan.calls}
    return {call: exchanges[-repeats[call] :] for call, exchanges in found.items()}


def _normalized(value: JsonValue, *, timed: bool) -> JsonValue:
    text = UUID.sub("<uuid>", jsonfmt.dumps(value))
    text = TASK_ID.sub("<task>", text)
    if timed:
        text = ISO.sub("<iso>", text)
    return jsonfmt.plain(jsonfmt.loads(text))


def _render(value: JsonValue, variables: dict[str, JsonValue]) -> JsonValue:
    if isinstance(value, str):
        match = re.fullmatch(r"\{\{(\w+)\}\}", value)
        return variables.get(match.group(1), value) if match else value
    if isinstance(value, list):
        return [_render(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, variables) for key, item in value.items()}
    return value


def _sent_during(exchange: Exchange) -> list[JsonValue]:
    """What the server sent on the call's stream before its answer (requests, notifications)."""
    sent: list[JsonValue] = []
    for event in exchange.stream:
        data = event.data if hasattr(event, "data") else None
        if not data:
            continue
        message = jsonfmt.loads(data)
        if isinstance(message, dict) and "method" in message:
            sent.append(message)
    return sent


def _numbered(messages: list[Any]) -> list[JsonValue]:
    """Server requests with their id as ``<id>``, which the server numbers per session.

    A frozen clock holds the request it sends 350 ms after initialization, so ids may shift.
    """
    return [
        {**message, "id": "<id>"} if isinstance(message, dict) and "id" in message else message
        for message in messages
    ]


def _answer(exchange: Exchange) -> JsonValue:
    reply = exchange.response.body.parsed() if exchange.response else None
    if not isinstance(reply, dict):
        return None
    return reply.get("result", reply.get("error"))


@pytest.mark.parametrize("name", PLANS)
async def test_recorded_session(name: str) -> None:
    settings = Settings()
    plan = read_model(settings.data_dir / "plans" / f"{name}.json", RecordingPlan)
    recorded = _recorded(RecordingStore(settings.data_dir / "recordings"), plan)
    app = build_app(settings_for_tests(Settings))
    capabilities = plan.client_capabilities if isinstance(plan.client_capabilities, dict) else None
    variables: dict[str, JsonValue] = {}
    mismatches: list[str] = []
    async with streaming_client(app) as client:
        wire = McpWireClient("http://fake.test/mcp", client)
        await wire.initialize(capabilities=capabilities)
        if plan.listen:
            wire.on_request = replier({})
            await wire.listen()
        try:
            for call in plan.calls:
                exchanges = recorded.get(call.id, [])
                for exchange in exchanges:
                    wire.on_request = replier(call.client_replies)
                    method = call.endpoint.removeprefix("MCP ").split(":", 1)[0]
                    params = _render(call.request.json_body, variables)
                    with at_recording_time(app, exchange):
                        rpc = await wire.call(method, params)
                    reply = rpc.response
                    during = [m for m in rpc.messages if isinstance(m, dict) and "method" in m]
                    expected_during = _normalized(_numbered(_sent_during(exchange)), timed=True)
                    if _normalized(_numbered(during), timed=True) != expected_during:
                        mismatches.append(
                            f"{call.id} ({exchange.id}) sent {during!r} != {expected_during!r}"
                        )
                    actual = (
                        reply.get("result", reply.get("error")) if isinstance(reply, dict) else None
                    )
                    for key, path in call.capture.items():
                        task = actual.get("task") if isinstance(actual, dict) else None
                        if path == "$.result.task.taskId" and isinstance(task, dict):
                            variables[key] = task["taskId"]
                    timed = name in MAYBE_TIMED
                    expected = _normalized(_answer(exchange), timed=timed)
                    if _normalized(actual, timed=timed) != expected:
                        mismatches.append(f"{call.id} ({exchange.id}): {actual!r} != {expected!r}")
        finally:
            await wire.close()
    assert not mismatches, "\n".join(mismatches)
