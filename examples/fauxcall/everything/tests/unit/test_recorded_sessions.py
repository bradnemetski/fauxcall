"""Every recording plan, replayed in order against the fake as the same kind of client.

``test_recorded.py`` replays single requests in a fresh session without client capabilities, so it
can't check what depends on the session: tools only offered to clients that sample, elicit or
have roots, answers built from what the client replied, session resources, toggles, tasks. Here
each plan runs as one session again: the same declared capabilities, the same client replies,
the GET stream held open when the recording did, and the fake's clock at the moment each call
was recorded. Every answer must equal the recorded one, and so must what the server sent during
each call (progress, log messages, its requests to the client), ids the server makes up (UUIDs,
task ids, session ids) aside.
"""

from __future__ import annotations

import base64
import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Final

import pytest
from fauxcall import jsonpath
from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import at_recording_time, settings_for_tests

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fauxcall.model.recording import Exchange

DATA: Final = Settings().data_dir
PLANS: Final = sorted((DATA / "plans").glob("*.json"))
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TASK_ID: Final = re.compile(r"\b[0-9a-f]{32}\b")
_PLACEHOLDER: Final = re.compile(r"\{\{(\w+)\}\}")
_ISO: Final = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
_LOCAL_TIME: Final = re.compile(r"\b\d{1,2}:\d\d:\d\d [AP]M\b")
_BLOB_PREFIX: Final = "UmVzb3VyY2Ug"  # base64 of "Resource " (dynamic blob resources)


def _recorded_by_call() -> dict[str, list[Exchange]]:
    found: dict[str, list[Exchange]] = defaultdict(list)
    for _, exchange in RecordingStore(DATA / "recordings").iter_exchanges():
        if exchange.plan_call:
            found[exchange.plan_call].append(exchange)
    return found


RECORDED: Final = _recorded_by_call()
PLAN_TEXTS: Final = {path.stem: json.loads(path.read_text()) for path in PLANS}


def _blobs_decoded(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _blobs_decoded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_blobs_decoded(item) for item in value]
    if isinstance(value, str) and value.startswith(_BLOB_PREFIX):
        return "blob:" + base64.b64decode(value).decode()
    return value


def normalized(value: Any) -> Any:
    """``value`` with the ids the server makes up, and its clock readings, as placeholders.

    The recording notes when the client sent a call; the server stamps it a few milliseconds
    later, so times are compared by the tests of the clock instead.
    """
    text = json.dumps(_blobs_decoded(value), ensure_ascii=False, sort_keys=False)
    text = _ISO.sub("<iso>", text)
    text = _LOCAL_TIME.sub("<time>", text)
    text = _UUID.sub("<uuid>", text)
    text = _TASK_ID.sub("<task-id>", text)
    return json.loads(text)


def filled(value: Any, variables: dict[str, Any]) -> Any:
    """``{{name}}`` placeholders replaced by captured values."""
    text = json.dumps(value)
    for name, captured in variables.items():
        text = text.replace("{{" + name + "}}", str(captured))
    assert not _PLACEHOLDER.search(text), text
    return json.loads(text)


def _server_messages(messages: Any) -> list[Any]:
    """What the server sent during a call besides its answer: notifications and requests to the
    client (sampling, elicitation, tasks/get …), by method and params (request ids differ)."""
    return [
        normalized({"method": m["method"], "params": m.get("params")})
        for m in messages
        if isinstance(m, dict) and "method" in m
    ]


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


def _method(endpoint: str) -> str:
    return endpoint.removeprefix("MCP ").split(":", 1)[0]


async def _replay_plan(app: FastAPI, plan_path: Path) -> list[str]:
    plan = PLAN_TEXTS[plan_path.stem]
    problems: list[str] = []
    variables: dict[str, Any] = {}
    runtime_of_app(app).reset()
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize(capabilities=plan.get("client_capabilities"))
        if plan.get("listen"):
            await wire.listen()
        try:
            for call in plan["calls"]:
                recorded = RECORDED.get(call["id"], [])
                wire.on_request = replier(call.get("client_replies", {}))
                captured = _PLACEHOLDER.search(json.dumps(call["request"]["json_body"]))
                for exchange in recorded:
                    sent = exchange.request.body.parsed()
                    # the request as recorded; with the fake's own ids where the plan captured some
                    params = (
                        filled(call["request"]["json_body"], variables)
                        if captured or not isinstance(sent, dict)
                        else sent.get("params")
                    )
                    with at_recording_time(app, exchange):
                        answer = await wire.call(_method(call["endpoint"]), params)
                    expected = exchange.response.body.parsed() if exchange.response else None
                    if not isinstance(expected, dict) or not isinstance(answer.response, dict):
                        problems.append(f"{call['id']}: no answer")
                        continue
                    for member in ("result", "error"):
                        want = normalized(expected.get(member))
                        got = normalized(answer.response.get(member))
                        if want != got:
                            problems.append(f"{call['id']} {member}:\n  real {want}\n  fake {got}")
                    want_sent = _server_messages(
                        json.loads(e.data) for e in exchange.stream if e.data
                    )
                    got_sent = _server_messages(answer.messages)
                    if want_sent != got_sent:
                        problems.append(
                            f"{call['id']} messages:\n  real {want_sent}\n  fake {got_sent}"
                        )
                    for name, path in call.get("capture", {}).items():
                        value = jsonpath.get_one(answer.response, path)
                        if value is not None:
                            variables[name] = value
        finally:
            await wire.close()
    return problems


@pytest.mark.parametrize("plan_path", PLANS, ids=[p.stem for p in PLANS])
async def test_plan_replays_as_recorded(app: FastAPI, plan_path: Path) -> None:
    problems = await _replay_plan(app, plan_path)
    assert not problems, "\n".join(problems)
