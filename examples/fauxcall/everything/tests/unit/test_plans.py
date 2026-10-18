"""Every recording plan, replayed against the fake in one session, answer by answer.

Unlike ``test_recorded.py`` (each request alone, in a fresh session without capabilities, shapes
only), this runs each plan the way it was recorded: the same client capabilities, the same
answers to the server's requests, captured task ids, the GET stream open when the plan listened,
the calls in order. Each answer must equal the recorded one *value for value*, once the values
that differ on every run are masked: UUIDs (session and elicitation ids), task ids, ISO
timestamps and times of day (also inside base64 blobs). Task timers run ten times faster.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Final

import pytest
from fauxcall import jsonfmt, jsonpath
from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.model.plan import RecordingPlan
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime.app import runtime_of_app
from fauxcall.runtime.templating import from_mapping, render
from fauxcall.storage import read_model
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything import tasks
from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fauxcall.types import JsonValue

DATA = Settings().data_dir
TICK: Final = 0.1
"""The task timers' scale in these replays (1 s stages take 0.1 s)."""
PACE_S: Final = 0.5
"""The recorder's pause between calls (two calls a second), scaled like the timers."""
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TASK_ID: Final = re.compile(r"\b[0-9a-f]{32}\b")
_ISO: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_TIME: Final = re.compile(r"\b\d{1,2}:\d{2}:\d{2} [AP]M\b")
_TARGET: Final = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}


def _mask_text(text: str) -> str:
    for pattern, mask in (
        (_UUID, "<uuid>"),
        (_TASK_ID, "<task>"),
        (_ISO, "<iso>"),
        (_TIME, "<time>"),
    ):
        text = pattern.sub(mask, text)
    return text


def masked(value: JsonValue) -> JsonValue:
    """``value`` with run-specific values masked (base64 texts decoded first)."""
    if isinstance(value, dict):
        return {key: masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [masked(item) for item in value]
    if isinstance(value, str):
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return _mask_text(value)
        return "base64:" + _mask_text(decoded) if decoded.startswith("Resource ") else value
    return jsonfmt.plain(value)


def _plans() -> list[RecordingPlan]:
    plans = [read_model(path, RecordingPlan) for path in sorted((DATA / "plans").glob("*.json"))]
    return [
        plan
        for plan in plans
        if plan.calls and all(c.endpoint.startswith("MCP ") for c in plan.calls)
    ]


def _recorded() -> dict[tuple[str, str], list[JsonValue]]:
    """(plan call id, endpoint) -> the recorded replies, in recording order."""
    replies: dict[tuple[str, str], list[JsonValue]] = defaultdict(list)
    for _, exchange in RecordingStore(DATA / "recordings").iter_exchanges():
        if exchange.plan_call and exchange.response is not None:
            replies[(exchange.plan_call, exchange.endpoint)].append(exchange.response.body.parsed())
    return replies


PLANS = _plans()
RECORDED = _recorded()


def _method(endpoint: str) -> str:
    return endpoint.removeprefix("MCP ").split(":", 1)[0]


def _params(endpoint: str, params: JsonValue) -> JsonValue:
    method, _, detail = endpoint.removeprefix("MCP ").partition(":")
    key = _TARGET.get(method)
    if key is None or not detail:
        return params
    if params is None:
        return {key: detail}
    if isinstance(params, dict) and key not in params:
        return {key: detail, **params}
    return params


def _answer(reply: JsonValue) -> JsonValue:
    if not isinstance(reply, dict):
        return reply
    return {member: reply[member] for member in ("result", "error") if member in reply}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(tasks, "TICK", TICK)
    built = build_app(settings_for_tests(Settings))
    runtime_of_app(built).reset(journal=True)
    return built


async def _replay(app: FastAPI, plan: RecordingPlan) -> list[str]:
    problems: list[str] = []
    variables: dict[str, Any] = {}
    seen: dict[tuple[str, str], int] = defaultdict(int)
    async with streaming_client(app) as http:
        wire = McpWireClient(f"{http.base_url}/mcp", http)
        capabilities = (
            plan.client_capabilities if isinstance(plan.client_capabilities, dict) else None
        )
        await wire.initialize(capabilities=capabilities)
        try:
            if plan.listen:
                wire.on_request = replier({})
                await wire.listen()
            for call in plan.calls:
                method = _method(call.endpoint)
                for index in range(call.repeat):
                    gap = PACE_S  # the recorder's pace overlaps a repeat's interval
                    if index and method.startswith("tasks/"):  # only tasks change with time
                        gap = max(PACE_S, call.interval_ms / 1000)
                    await asyncio.sleep(gap * TICK)
                    params = render(call.request.json_body, from_mapping(variables))
                    wire.on_request = replier(call.client_replies)
                    rpc = await wire.call(method, _params(call.endpoint, params))
                    key = (call.id, call.endpoint)
                    recorded = RECORDED.get(key, [])
                    position = seen[key]
                    seen[key] += 1
                    for name, path in call.capture.items():
                        captured = jsonpath.get_one(rpc.response, path)
                        if captured is not None:
                            variables[name] = captured
                    if position >= len(recorded):
                        continue
                    want, got = masked(_answer(recorded[position])), masked(_answer(rpc.response))
                    if want != got:
                        problems.append(
                            f"{call.id}#{index + 1} ({call.endpoint}):\n"
                            f"    recorded {json.dumps(want, ensure_ascii=False)[:600]}\n"
                            f"    fake     {json.dumps(got, ensure_ascii=False)[:600]}"
                        )
        finally:
            if plan.listen:
                await wire.stop_listening()
            await wire.close()
    return problems


@pytest.mark.parametrize("plan", PLANS, ids=[plan.name for plan in PLANS])
async def test_plan_replays_as_recorded(app: FastAPI, plan: RecordingPlan) -> None:
    problems = await _replay(app, plan)
    assert not problems, "\n".join(problems)
