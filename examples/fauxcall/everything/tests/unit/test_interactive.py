"""Tools that ask the client, and stateful flows, driven in-process with an answering client."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.runtime.recordings import RecordingIndex
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx2
    from fauxcall.types import JsonObject

RECORDINGS = RecordingIndex(Path(Settings().data_dir) / "recordings")
ALL: JsonObject = {
    "roots": {"listChanged": True},
    "sampling": {},
    "elicitation": {"form": {}, "url": {}},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


class Session:
    """One MCP client session against the in-process fake."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self.http = http
        self.wire = McpWireClient("http://fake.test/mcp", http)

    async def open(self, capabilities: JsonObject | None = None, **replies: Any) -> None:
        await self.wire.initialize(capabilities=capabilities)
        self.answer(**replies)

    def answer(self, **replies: Any) -> None:
        self.wire.on_request = replier(
            {key.replace("__", "/"): value for key, value in replies.items()}
        )

    async def call(self, name: str, arguments: Any = None, **params: Any) -> Any:
        body: dict[str, Any] = {"name": name, **params}
        if arguments is not None:
            body["arguments"] = arguments
        return (await self.wire.call("tools/call", body)).response

    async def request(self, method: str, params: Any = None) -> Any:
        return (await self.wire.call(method, params)).response

    async def advance(self, seconds: float) -> None:
        reply = await self.http.post(
            "/__fauxcall/clock", json={"op": "advance", "seconds": seconds}
        )
        assert reply.status_code == 200, reply.text


def text(reply: Any, index: int = 0) -> str:
    return str(reply["result"]["content"][index]["text"])


@pytest.fixture
async def session() -> AsyncIterator[Session]:
    app = build_app(settings_for_tests(Settings))
    async with streaming_client(app) as http:
        client = Session(http)
        try:
            yield client
        finally:
            await client.wire.close()


async def test_sampling_refusals_and_bad_answers(session: Session) -> None:
    await session.open({"sampling": {}}, sampling__createMessage={"model": 1, "role": "robot"})
    broken = await session.call("trigger-sampling-request", {"prompt": "x"})
    assert '"path": [\n      "model"\n    ]' in text(broken)
    assert '"code": "invalid_value"' in text(broken)
    session.answer(sampling__createMessage={"error": {"code": -1, "message": "no"}})
    assert (
        text(await session.call("trigger-sampling-request", {"prompt": "x"})) == "MCP error -1: no"
    )
    invalid = await session.call("trigger-sampling-request", {"maxTokens": 5})
    assert "expected string, received undefined at prompt" in text(invalid)


async def test_elicitation_shows_every_known_field(session: Session) -> None:
    content = {
        "name": "Ada",
        "check": True,
        "color": "red",
        "email": "a@b.c",
        "homepage": "https://a.b",
        "birthdate": "1815-12-10",
        "integer": 0,
        "number": 1.0,
        "petType": "cat",
    }
    await session.open(
        {"elicitation": {}}, elicitation__create={"action": "accept", "content": content}
    )
    shown = await session.call("trigger-elicitation-request", {})
    assert text(shown, 1) == (
        "User inputs:\n- Name: Ada\n- Agreed to terms: true\n- Favorite Color: red\n"
        "- Email: a@b.c\n- Homepage: https://a.b\n- Birthdate: 1815-12-10\n"
        "- Favorite Integer: 0\n- Favorite Number: 1\n- Pet Type: cat"
    )
    session.answer(elicitation__create={"action": "cancel"})
    assert text(await session.call("trigger-elicitation-request", {})).startswith("⚠️")
    session.answer(elicitation__create={"error": {"code": -32601, "message": "Method not found"}})
    assert (
        text(await session.call("trigger-elicitation-request", {}))
        == "MCP error -32601: Method not found"
    )
    session.answer(elicitation__create={"action": "perhaps"})
    assert '"invalid_value"' in text(await session.call("trigger-elicitation-request", {}))


async def test_url_elicitation_outcomes(session: Session) -> None:
    await session.open({"elicitation": {"url": {}}}, elicitation__create={"action": "cancel"})
    cancelled = await session.call(
        "trigger-url-elicitation", {"url": "https://a.example", "elicitationId": "e"}
    )
    assert text(cancelled) == "⚠️ User cancelled the URL elicitation (Elicitation ID: e)."
    session.answer(elicitation__create={"error": {"code": -32000, "message": "closed"}})
    refused = await session.call("trigger-url-elicitation", {"url": "https://a.example"})
    assert text(refused) == "MCP error -32000: closed"
    session.answer(elicitation__create={"action": 5})
    assert '"invalid_value"' in text(
        await session.call("trigger-url-elicitation", {"url": "https://a.example"})
    )
    random_id = await session.call(
        "trigger-url-elicitation", {"url": "https://b.example", "errorPath": True}
    )
    assert random_id["error"]["code"] == -32042


async def test_roots_follow_the_client(session: Session) -> None:
    await session.open(
        {"roots": {"listChanged": True}}, roots__list={"roots": [{"uri": "https://not-a-file"}]}
    )
    await session.wire.listen()
    await session.advance(1)
    await asyncio.sleep(0.05)
    invalid = await session.call("get-roots-list", {})
    assert text(invalid).startswith("The client supports roots but no roots")
    session.answer(roots__list={"roots": [{"uri": "file:///a"}]})
    assert "1. Unnamed Root\n   URI: file:///a" in text(await session.call("get-roots-list", {}))
    session.answer(
        roots__list={
            "roots": [{"uri": "file:///b", "name": "B"}, {"uri": "file:///c", "name": "C"}]
        }
    )
    await session.wire.notify("notifications/roots/list_changed")
    for _ in range(50):
        listed = await session.call("get-roots-list", {})
        if "(2 total)" in text(listed):
            break
        await asyncio.sleep(0.02)
    assert "1. B\n   URI: file:///b\n\n2. C\n   URI: file:///c" in text(listed)


async def test_roots_without_a_get_stream_time_out(session: Session) -> None:
    await session.open({"roots": {}}, roots__list={"roots": [{"uri": "file:///a"}]})
    pending = asyncio.ensure_future(session.call("get-roots-list", {}))
    for _ in range(20):
        await asyncio.sleep(0.02)
        await session.advance(61)
        if pending.done():
            break
    assert text(await pending).startswith("The client supports roots but no roots")


async def test_async_sampling_outcomes(session: Session) -> None:
    await session.open(
        ALL,
        sampling__createMessage={"task": {"taskId": "t1", "status": "working"}},
        tasks__get={"taskId": "t1", "status": "failed"},
    )
    failed = await session.call("trigger-sampling-request-async", {"prompt": "p"})
    assert text(failed) == "[FAILED] No message\n\nProgress:\nTask created: t1\nPoll 1: failed"
    session.answer(sampling__createMessage={"error": {"code": -5, "message": "busy"}})
    assert (
        text(await session.call("trigger-sampling-request-async", {"prompt": "p"}))
        == "MCP error -5: busy"
    )
    assert "received undefined at prompt" in text(
        await session.call("trigger-sampling-request-async", {})
    )


async def test_async_elicitation_outcomes(session: Session) -> None:
    await session.open(
        ALL,
        elicitation__create={"task": {"taskId": "f1", "status": "working"}},
        tasks__get={"taskId": "f1", "status": "completed"},
        tasks__result={"action": "decline"},
    )
    declined = await session.call("trigger-elicitation-request-async", {})
    assert text(declined) == "[DECLINED] User declined to provide the requested information."
    session.answer(
        elicitation__create={"task": {"taskId": "f2", "status": "working"}},
        tasks__get={"taskId": "f2", "status": "completed"},
        tasks__result={"action": "accept", "content": {"name": "Lin", "favoriteColor": "Red"}},
    )
    accepted = await session.call("trigger-elicitation-request-async", {})
    assert text(accepted, 1) == "User inputs:\n- Name: Lin\n- Favorite Color: Red"
    session.answer(
        elicitation__create={"task": {"taskId": "f3", "status": "working"}},
        tasks__get={"taskId": "f3", "status": "completed"},
        tasks__result={"action": "cancel"},
    )
    assert text(await session.call("trigger-elicitation-request-async", {})).startswith(
        "[CANCELLED]"
    )
    session.answer(elicitation__create={"action": "accept"})
    synced = await session.call("trigger-elicitation-request-async", {})
    assert text(synced) == '[SYNC] Client executed synchronously:\n{\n  "action": "accept"\n}'
    session.answer(elicitation__create={"error": {"code": -9, "message": "nope"}})
    assert text(await session.call("trigger-elicitation-request-async", {})) == "MCP error -9: nope"


async def test_research_pages_cancels_and_expires(session: Session) -> None:
    await session.open()
    created = [
        (await session.call("simulate-research-query", {"topic": f"t{i}"}, task={}))["result"][
            "task"
        ]["taskId"]
        for i in range(11)
    ]
    first = (await session.request("tasks/list", {}))["result"]
    assert len(first["tasks"]) == 10
    assert first["nextCursor"] == created[9]
    rest = (await session.request("tasks/list", {"cursor": first["nextCursor"]}))["result"]
    assert [t["taskId"] for t in rest["tasks"]] == [created[10]]
    cancelled = (await session.request("tasks/cancel", {"taskId": created[0]}))["result"]
    assert cancelled["status"] == "cancelled"
    await session.advance(301)
    gone = await session.request("tasks/get", {"taskId": created[0]})
    assert gone["error"]["message"] == "MCP error -32602: Failed to retrieve task: Task not found"
    assert (await session.request("tasks/list", {}))["result"]["tasks"] == []


async def test_research_tool_before_initialized_is_not_there(session: Session) -> None:
    await session.wire.initialize(announce=False)
    early = await session.call("simulate-research-query", {"topic": "x"})
    assert early["result"]["isError"] is True
    as_task = await session.call("simulate-research-query", {"topic": "x"}, task={})
    assert as_task["error"]["message"].startswith("MCP error -32602: Invalid task creation result")


def recorded_text(exchange_id: str) -> str:
    """The text of a recorded tool result (the last message of its stream)."""
    exchange = RECORDINGS.get(exchange_id)
    assert exchange is not None
    reply: Any = exchange.response.body.parsed() if exchange.response else None
    return str(reply["result"]["content"][0]["text"])


async def test_async_answers_are_validated_like_zod(session: Session) -> None:
    await session.open(ALL, sampling__createMessage={})
    empty = await session.call("trigger-sampling-request-async", {"prompt": "empty"})
    assert text(empty) == recorded_text("000526")
    session.answer(sampling__createMessage={"task": {"taskId": "x1"}})
    no_status = await session.call("trigger-sampling-request-async", {"prompt": "nostatus"})
    assert text(no_status) == recorded_text("000527")
    session.answer(
        sampling__createMessage={"task": {"taskId": "x2", "status": "working"}},
        tasks__get={"taskId": "x2"},
    )
    bad_poll = await session.call("trigger-sampling-request-async", {"prompt": "pollbad"})
    assert text(bad_poll) == recorded_text("000528")
    session.answer(elicitation__create={"action": 3})
    bad_action = await session.call("trigger-elicitation-request-async", {})
    assert '"path": [\n            "action"\n          ]' in text(bad_action)
    session.answer(elicitation__create={"action": "accept", "content": {"name": {"first": "Ada"}}})
    nested = await session.call("trigger-elicitation-request", {})
    assert text(nested) == recorded_text("000529")
    session.answer(elicitation__create={"action": "accept", "content": {"tags": ["a", 1]}})
    mixed = await session.call("trigger-elicitation-request", {})
    assert '"expected": "string"' in text(mixed)
    assert '"path": [\n            1\n          ]' in text(mixed)


async def test_a_cancelled_async_tool_stops_polling(session: Session) -> None:
    await session.open(
        ALL,
        elicitation__create={"task": {"taskId": "c1", "status": "input_required"}},
        tasks__get={"taskId": "c1", "status": "input_required"},
    )
    asked: list[str] = []
    answering = session.wire.on_request
    assert answering is not None

    async def counting(request: Any) -> Any:
        asked.append(str(request.get("method")))
        return await answering(request)

    session.wire.on_request = counting
    call = await session.wire.call(
        "tools/call",
        {"name": "trigger-elicitation-request-async", "arguments": {}},
        cancel_after=1.5,
    )
    assert call.cancelled_after_ms is not None
    await asyncio.sleep(1.2)
    assert asked == ["elicitation/create", "tasks/get"]  # nothing after the cancel (000530)


async def test_numeric_interpretation_fails_the_task(session: Session) -> None:
    await session.open(
        {"elicitation": {}},
        elicitation__create={"action": "accept", "content": {"interpretation": 5}},
    )
    created = await session.call(
        "simulate-research-query", {"topic": "numbers", "ambiguous": True}, task={}
    )
    task_id = created["result"]["task"]["taskId"]
    finished = await session.request("tasks/result", {"taskId": task_id})
    assert finished["error"] == {"code": -32603, "message": f"Task {task_id} has no result stored"}
    failed = (await session.request("tasks/get", {"taskId": task_id}))["result"]
    assert failed["status"] == "failed"
    assert failed["statusMessage"] == "TypeError: state.clarification.includes is not a function"


async def test_tasks_result_answers_on_its_one_second_grid(session: Session) -> None:
    await session.open()
    created = await session.call("simulate-research-query", {"topic": "grid"}, task={})
    task_id = created["result"]["task"]["taskId"]
    await asyncio.sleep(0.6)
    started = time.monotonic()
    done = await session.request("tasks/result", {"taskId": task_id})
    elapsed = time.monotonic() - started
    assert done["result"]["content"][0]["text"].startswith("# Research Report: grid")
    # the task ends 4 s after creation; ticks at 0.6 + n s: the answer at the 4.6 s tick
    # (000507: called later, answered 3009 ms in, on its own grid)
    assert 3.9 < elapsed < 4.4, elapsed


async def test_a_timed_out_clarification_is_still_delivered(session: Session) -> None:
    await session.open(
        {"elicitation": {}},
        elicitation__create={"action": "accept", "content": {"interpretation": "historical"}},
    )
    created = await session.call(
        "simulate-research-query", {"topic": "stale", "ambiguous": True}, task={}
    )
    task_id = created["result"]["task"]["taskId"]
    for _ in range(60):
        status = (await session.request("tasks/get", {"taskId": task_id}))["result"]["status"]
        if status == "input_required":
            break
        await asyncio.sleep(0.1)
    await session.advance(61)
    for _ in range(60):
        status = (await session.request("tasks/get", {"taskId": task_id}))["result"]["status"]
        if status == "completed":
            break
        await asyncio.sleep(0.1)
    asked: list[str] = []
    answering = session.wire.on_request
    assert answering is not None

    async def counting(request: Any) -> Any:
        asked.append(str(request.get("method")))
        return await answering(request)

    session.wire.on_request = counting
    late = await session.request("tasks/result", {"taskId": task_id})
    assert "(technical (default - elicitation unavailable))" in late["result"]["content"][0]["text"]
    assert asked == ["elicitation/create"]  # the stale request goes out first (000523)


async def test_tasks_list_cursor_must_be_a_string(session: Session) -> None:
    await session.open()
    refused = await session.request("tasks/list", {"cursor": 5})
    assert refused["error"]["code"] == -32603
    assert "expected string, received number" in refused["error"]["message"]


async def test_tasks_on_tools_that_are_not_there(session: Session) -> None:
    await session.open()
    for name in ("no-such-tool", "trigger-elicitation-request"):
        refused = await session.call(name, {}, task={})
        assert refused["error"]["message"].startswith(
            "MCP error -32602: Invalid task creation result"
        )


async def test_urls_are_trimmed_before_use(session: Session) -> None:
    await session.open({"elicitation": {"url": {}}}, elicitation__create={"action": "accept"})
    padded = await session.call(
        "trigger-url-elicitation",
        {"url": "  https://example.com/padded  ", "elicitationId": "trim-1"},
    )
    assert text(padded) == recorded_text("000490")
    ftp = await session.call(
        "gzip-file-as-resource", {"name": "t.gz", "data": " ftp://x.example/a "}
    )
    assert text(ftp) == recorded_text("000492")


async def test_tools_call_request_checks(session: Session) -> None:
    await session.open()
    nameless = await session.request("tools/call", {"name": 7, "arguments": None})
    assert '"received number"' not in nameless["error"]["message"]
    assert "expected string, received number" in nameless["error"]["message"]
    assert "expected record, received null" in nameless["error"]["message"]
    assert (await session.call("get-env"))["result"]["content"][0]["type"] == "text"


async def test_completion_and_prompt_odd_inputs(session: Session) -> None:
    await session.open()
    resource_ref = {"type": "ref/resource", "uri": "demo://resource/dynamic/text/{resourceId}"}
    other = await session.request(
        "completion/complete", {"ref": resource_ref, "argument": {"name": "x", "value": "1"}}
    )
    assert other["result"] == {"completion": {"values": [], "hasMore": False}}
    missing = await session.request(
        "completion/complete", {"ref": {"type": "ref/prompt", "name": "args-prompt"}, "argument": 3}
    )
    assert "expected object, received number" in missing["error"]["message"]
    nameless = await session.request("prompts/get", {"name": "simple-prompt", "arguments": "x"})
    assert "expected record, received string" in nameless["error"]["message"]
    await session.call("gzip-file-as-resource", {"name": "k.gz", "data": "data:,k"})
    listed = await session.request(
        "completion/complete",
        {
            "ref": {"type": "ref/resource", "uri": "demo://resource/session/k.gz"},
            "argument": {"name": "a", "value": ""},
        },
    )
    assert listed["result"]["completion"]["values"] == []


async def test_subscriptions_and_logging_toggles(session: Session) -> None:
    await session.open()
    await session.wire.listen()
    await session.request("logging/setLevel", {"level": "emergency"})
    await session.request("resources/subscribe", {"uri": "demo://x"})
    await session.request("resources/unsubscribe", {"uri": "demo://never"})
    started = await session.call("toggle-simulated-logging", {})
    assert text(started).startswith("Started simulated")
    stopped = await session.call("toggle-simulated-logging", {})
    assert text(stopped).startswith("Stopped simulated logging")
    on = await session.call("toggle-subscriber-updates", {})
    assert text(on).startswith("Started simulated resource updated")
    await session.advance(5)
    off = await session.call("toggle-subscriber-updates", {})
    assert text(off).startswith("Stopped simulated resource updates")
