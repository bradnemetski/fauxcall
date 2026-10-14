"""The fake's own logic: JavaScript formatting, the server clock, tasks, roots, sessions."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from fauxcall.mcpwire.client import McpWireClient, replier
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything import jsfmt
from fauxcall_everything.app import build_app
from fauxcall_everything.jsfmt import Field
from fauxcall_everything.settings import Settings
from fauxcall_everything.state import iso_ms, local_time

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from fauxcall.mcpwire.client import RpcCall

ALL_CAPS: dict[str, Any] = {
    "sampling": {},
    "elicitation": {"form": {}, "url": {}},
    "roots": {"listChanged": True},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


def ns(text: str) -> int:
    moment = datetime.fromisoformat(text).replace(tzinfo=UTC)
    whole = int(moment.replace(microsecond=0).timestamp())
    return whole * 1_000_000_000 + moment.microsecond * 1000


@pytest.fixture
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


def freeze(app: FastAPI, at_ns: int) -> None:
    runtime_of_app(app).clocks.override("default").freeze(at_ns)


@contextlib.asynccontextmanager
async def session(
    app: FastAPI, capabilities: dict[str, Any] | None = None, replies: dict[str, Any] | None = None
) -> AsyncIterator[McpWireClient]:
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        wire.on_request = replier(replies or {})
        await wire.initialize(capabilities=capabilities)
        try:
            yield wire
        finally:
            await wire.close()


async def call_tool(wire: McpWireClient, name: str, arguments: dict[str, Any], **extra: Any) -> Any:
    reply = await wire.call("tools/call", {"name": name, "arguments": arguments, **extra})
    assert isinstance(reply.response, dict)
    return reply.response


def result_of(call: RpcCall) -> Any:
    return call.result


def error_of(call: RpcCall) -> Any:
    return call.error


def text_of(response: Any, index: int = 0) -> str:
    return str(response["result"]["content"][index]["text"])


# -- JavaScript formatting -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (True, "true"),
        (math.nan, "NaN"),
        (-math.inf, "-Infinity"),
        (0.0, "0"),
        (-0.0, "0"),
        (123e-20, "1.23e-18"),
        (0.000001, "0.000001"),
        (1.5e300, "1.5e+300"),
        (12, "12"),
    ],
)
def test_number_spelling(value: float, shown: str) -> None:
    assert jsfmt.number(value) == shown


def test_to_string_follows_javascript() -> None:
    assert jsfmt.to_string(None) == "null"
    assert jsfmt.to_string(value=False) == "false"
    assert jsfmt.to_string(2.0) == "2"
    assert jsfmt.to_string(["a", None, 3]) == "a,,3"
    assert jsfmt.to_string({"a": 1}) == "[object Object]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0b101", 5.0), ("0o17", 15.0), ("Infinity", math.inf), ("-Infinity", -math.inf), ("  ", 0.0)],
)
def test_to_number_follows_javascript(text: str, expected: float) -> None:
    assert jsfmt.to_number(text) == expected


def test_to_number_rejects_what_javascript_rejects() -> None:
    assert math.isnan(jsfmt.to_number("0xZZ"))
    assert math.isnan(jsfmt.to_number("1,000"))


def test_stringify_writes_non_finite_numbers_as_null() -> None:
    assert jsfmt.stringify([math.inf, []]) == "[\n  null,\n  []\n]"


@pytest.mark.parametrize(
    ("url", "valid"),
    [
        ("http://example.com:8080/x", True),
        ("http://example.com:99999/", False),
        ("http://a b/", False),
    ],
)
def test_url_validity(url: str, valid: bool) -> None:
    assert jsfmt.is_url(url) == valid


def test_href_leaves_text_without_scheme() -> None:
    assert jsfmt.href("no scheme here") == "no scheme here"


def test_zod_checks() -> None:
    schema = {"n": Field("number", minimum=1, maximum=3), "b": Field("boolean")}
    assert jsfmt.check(schema, {"n": 4})[1] == ["Too big: expected number to be <=3 at n"]
    assert jsfmt.check(schema, {"n": True})[1] == [
        "Invalid input: expected number, received boolean at n"
    ]
    assert jsfmt.check(schema, {"b": {}})[1] == [
        "Invalid input: expected boolean, received object at b"
    ]
    assert math.isnan(jsfmt.as_float("x"))


# -- the server's clock ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instant", "shown"),
    [
        ("2026-10-14T01:40:47", "9:40:47 PM"),
        ("2026-10-14T16:00:00", "12:00:00 PM"),
        ("2026-10-14T04:05:09", "12:05:09 AM"),
        ("2026-01-15T14:30:00", "9:30:00 AM"),  # standard time: UTC-5
    ],
)
def test_local_time_is_the_servers(instant: str, shown: str) -> None:
    assert local_time(ns(instant)) == shown


def test_iso_ms() -> None:
    assert iso_ms(ns("2026-10-14T01:49:35.698")) == "2026-10-14T01:49:35.698Z"


async def test_dynamic_resources_are_stamped_with_the_fake_clock(app: FastAPI) -> None:
    freeze(app, ns("2026-10-14T13:07:02"))
    async with session(app) as wire:
        text = await wire.call("resources/read", {"uri": "demo://resource/dynamic/text/12"})
        blob = await wire.call("resources/read", {"uri": "demo://resource/dynamic/blob/3"})
    assert isinstance(result_of(text), dict)
    assert isinstance(result_of(blob), dict)
    assert result_of(text)["contents"][0]["text"] == (
        "Resource 12: This is a plaintext resource created at 9:07:02 AM"
    )
    decoded = base64.b64decode(result_of(blob)["contents"][0]["blob"]).decode()
    assert decoded == "Resource 3: This is a base64 blob created at 9:07:02 AM"


# -- research tasks --------------------------------------------------------------------------------


async def test_research_task_moves_through_its_stages(app: FastAPI) -> None:
    start = ns("2026-10-14T02:00:00")
    freeze(app, start)
    async with session(app) as wire:
        created = await call_tool(wire, "simulate-research-query", {"topic": "x"}, task={})
        task_id = created["result"]["task"]["taskId"]
        seen = []
        for offset in (0.5, 1.5, 2.5, 3.5, 4.5):
            freeze(app, start + int(offset * 1e9))
            got = await wire.call("tasks/get", {"taskId": task_id})
            assert isinstance(result_of(got), dict)
            seen.append((result_of(got)["status"], result_of(got)["statusMessage"]))
        refused = await wire.call("tasks/cancel", {"taskId": task_id})
    assert seen == [
        ("working", "Gathering sources..."),
        ("working", "Analyzing content..."),
        ("working", "Synthesizing findings..."),
        ("working", "Generating report..."),
        ("completed", "Generating report..."),
    ]
    assert error_of(refused) == {
        "code": -32602,
        "message": "MCP error -32602: Cannot cancel task in terminal status: completed",
    }


async def test_ambiguous_research_without_an_answer_uses_the_default(app: FastAPI) -> None:
    # the client declares elicitation but answers "Method not found"
    async with session(app, {"elicitation": {}}) as wire:
        created = await call_tool(
            wire, "simulate-research-query", {"topic": "Python", "ambiguous": True}, task={}
        )
        task_id = created["result"]["task"]["taskId"]
        result = await wire.call("tasks/result", {"taskId": task_id})
    assert isinstance(result_of(result), dict)
    report = result_of(result)["content"][0]["text"]
    assert report.startswith(
        "# Research Report: Python (technical (default - elicitation unavailable))"
    )
    assert "**Note:** Elicitation failed and a default interpretation was used." in report


async def test_research_clarification_cancelled(app: FastAPI) -> None:
    replies = {"elicitation/create": {"action": "cancel"}}
    async with session(app, {"elicitation": {}}, replies) as wire:
        created = await call_tool(
            wire, "simulate-research-query", {"topic": "jazz", "ambiguous": True}, task={}
        )
        result = await wire.call("tasks/result", {"taskId": created["result"]["task"]["taskId"]})
    assert isinstance(result_of(result), dict)
    assert (
        "(User cancelled - using default interpretation)" in result_of(result)["content"][0]["text"]
    )


async def test_research_accepted_without_selection(app: FastAPI) -> None:
    replies = {"elicitation/create": {"action": "accept", "content": {}}}
    async with session(app, {"elicitation": {}}, replies) as wire:
        created = await call_tool(
            wire, "simulate-research-query", {"topic": "jazz", "ambiguous": True}, task={}
        )
        result = await wire.call("tasks/result", {"taskId": created["result"]["task"]["taskId"]})
    assert isinstance(result_of(result), dict)
    assert "(User accepted without selection)" in result_of(result)["content"][0]["text"]


async def test_cancelled_while_waiting_for_its_result(app: FastAPI) -> None:
    async with session(app) as wire:
        created = await call_tool(wire, "simulate-research-query", {"topic": "x"}, task={})
        task_id = created["result"]["task"]["taskId"]
        await wire.call("tasks/cancel", {"taskId": task_id})
        result = await wire.call("tasks/result", {"taskId": task_id})
        missing = await wire.call("tasks/get", {})
    assert error_of(result) == {"code": -32603, "message": f"Task {task_id} has no result stored"}
    assert isinstance(error_of(missing), dict)
    assert '"path": [\n      "params",\n      "taskId"\n    ]' in error_of(missing)["message"]


# -- roots -----------------------------------------------------------------------------------------


async def test_roots_are_asked_by_the_tool_and_again_when_they_change(
    app: FastAPI,
) -> None:
    first = {"roots/list": {"roots": [{"uri": "file:///a", "name": "a"}]}}
    async with session(app, {"roots": {"listChanged": True}}, first) as wire:
        await wire.listen()
        for _ in range(100):  # the initial sync, 350 ms after initialized (fast mode: at once)
            listed = await call_tool(wire, "get-roots-list", {})
            if "Current MCP Roots" in text_of(listed):
                break
            await asyncio.sleep(0.01)
        wire.on_request = replier({"roots/list": {"roots": []}})
        await wire.notify("notifications/roots/list_changed")
        for _ in range(100):
            again = await call_tool(wire, "get-roots-list", {})
            if "no roots are currently configured" in text_of(again):
                break
            await asyncio.sleep(0.01)
        heard = await wire.stop_listening()
    assert "1. a\n   URI: file:///a" in text_of(listed)
    assert "no roots are currently configured" in text_of(again)
    assert heard is not None
    logged: list[Any] = [
        m
        for m in heard.messages
        if isinstance(m, dict) and m.get("method") == "notifications/message"
    ]
    assert [m["params"]["data"] for m in logged] == [
        "Roots updated: 1 root(s) received from client",
        "Roots updated: 0 root(s) received from client",
    ]


async def test_roots_without_a_get_stream_wait_out_the_timeout(app: FastAPI) -> None:
    async with session(app, {"roots": {}}) as wire:
        listed = await call_tool(wire, "get-roots-list", {})
    assert "no roots are currently configured" in text_of(listed)
    assert any(pause > 59 for pause in runtime_of_app(app).pauses)


async def test_roots_refused_by_the_client(app: FastAPI) -> None:
    replies = {"roots/list": {"error": {"code": -1, "message": "no"}}, "x": {}}
    async with session(app, {"roots": {}}, replies) as wire:
        await wire.listen()
        listed = await call_tool(wire, "get-roots-list", {})
    assert "no roots are currently configured" in text_of(listed)


async def test_roots_answer_without_a_list_is_ignored(app: FastAPI) -> None:
    async with session(app, {"roots": {}}, {"roots/list": {}}) as wire:
        await wire.listen()
        listed = await call_tool(wire, "get-roots-list", {})
        await asyncio.sleep(0.05)
        heard = await wire.stop_listening()
    assert "no roots are currently configured" in text_of(listed)
    assert heard is not None
    sent = [m.get("method") for m in heard.messages if isinstance(m, dict)]
    assert sent == ["roots/list"]  # the request only: no log message follows


# -- toggles and subscriptions ---------------------------------------------------------------------


async def test_toggles_push_on_the_get_stream(app: FastAPI) -> None:
    uri = "demo://resource/dynamic/text/4"
    async with session(app) as wire:
        await wire.listen()
        await wire.call("resources/subscribe", {"uri": uri})
        started = await call_tool(wire, "toggle-subscriber-updates", {})
        await asyncio.sleep(0.05)
        stopped = await call_tool(wire, "toggle-subscriber-updates", {})
        await call_tool(wire, "toggle-simulated-logging", {})
        await asyncio.sleep(0.05)
        await call_tool(wire, "toggle-simulated-logging", {})
        await wire.call("resources/unsubscribe", {"uri": uri})
        await asyncio.sleep(0.05)
        heard = await wire.stop_listening()
        sid = wire.session_id
    assert text_of(started).startswith(
        f"Started simulated resource updated notifications for session {sid} "
    )
    assert text_of(stopped) == f"Stopped simulated resource updates for session {sid}"
    assert heard is not None
    sent: list[Any] = [m for m in heard.messages if isinstance(m, dict)]
    assert sent[0]["params"] == {
        "level": "info",
        "data": f"Received Subscribe Resource request for URI: {uri} from session {sid}",
    }
    assert sent[1] == {
        "jsonrpc": "2.0",
        "method": "notifications/resources/updated",
        "params": {"uri": uri},
    }  # sent at once, then every 5 s
    assert sent[2]["method"] == "notifications/message"  # the first random log, at once
    assert sent[2]["params"]["data"].endswith(f"-level message - SessionId {sid}") or sent[2][
        "params"
    ]["data"].startswith("Alert level-message")
    assert sent[-1]["params"]["data"] == (
        f"Received Unsubscribe Resource request: {uri} from session {sid}"
    )
    assert 5.0 in runtime_of_app(app).pauses


async def test_a_late_get_stream_gets_no_unprompted_roots_request(app: FastAPI) -> None:
    replies = {"roots/list": {"roots": [{"uri": "file:///r"}]}}
    async with session(app, {"roots": {}}, replies) as wire:
        await asyncio.sleep(0.05)  # past the server's 350 ms request (fast mode: at once)
        await wire.listen()
        await asyncio.sleep(0.05)
        heard = await wire.stop_listening()
    assert heard is not None
    assert heard.messages == []


async def test_tasks_expire_and_page(app: FastAPI) -> None:
    start = ns("2026-10-14T03:00:00")
    freeze(app, start)
    async with session(app) as wire:
        ids = []
        for n in range(12):
            created = await call_tool(wire, "simulate-research-query", {"topic": f"t{n}"}, task={})
            ids.append(created["result"]["task"]["taskId"])
        first = await wire.call("tasks/list", {})
        second = await wire.call("tasks/list", {"cursor": result_of(first)["nextCursor"]})
        bad = await wire.call("tasks/list", {"cursor": "nope"})
        await wire.call("tasks/cancel", {"taskId": ids[0]})
        freeze(app, start + 200 * 1_000_000_000)  # all completed at +4 s: kept until +304 s
        assert len(result_of(await wire.call("tasks/list", {}))["tasks"]) == 10
        freeze(app, start + 305 * 1_000_000_000)
        gone = await wire.call("tasks/get", {"taskId": ids[5]})
        result = await wire.call("tasks/result", {"taskId": ids[5]})
        empty = await wire.call("tasks/list", {})
    assert [t["taskId"] for t in result_of(first)["tasks"]] == ids[:10]
    assert result_of(first)["nextCursor"] == ids[9]
    assert list(result_of(first)) == ["tasks", "nextCursor", "_meta"]
    assert [t["taskId"] for t in result_of(second)["tasks"]] == ids[10:]
    assert (
        error_of(bad)["message"] == "MCP error -32602: Failed to list tasks: Invalid cursor: nope"
    )
    assert error_of(gone)["message"] == "MCP error -32602: Failed to retrieve task: Task not found"
    assert error_of(result)["message"] == f"MCP error -32602: Task not found: {ids[5]}"
    assert result_of(empty) == {"tasks": [], "_meta": {}}


async def test_protocol_versions(app: FastAPI) -> None:
    async with streaming_client(app) as http:
        for asked, answered in (("2024-10-07", "2024-10-07"), ("2099-01-01", "2025-11-25")):
            wire = McpWireClient("http://fake.test/mcp", http)
            init = await wire.initialize(protocol_version=asked)
            await wire.close()
            assert result_of(init)["protocolVersion"] == answered


async def test_review_edges(app: FastAPI) -> None:
    async with session(app, {"elicitation": {"form": {}, "url": {}}}) as wire:
        comma = await wire.call("resources/read", {"uri": "demo://resource/dynamic/text/1,2"})
        await call_tool(wire, "gzip-file-as-resource", {"name": "c.gz", "data": " data:,x "})
        completion = await wire.call(
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "demo://resource/session/c.gz"},
                "argument": {"name": "x", "value": ""},
            },
        )
        ipv6 = await call_tool(
            wire, "gzip-file-as-resource", {"name": "v.gz", "data": "http://[::1]:1/x"}
        )
        huge = await call_tool(
            wire, "trigger-long-running-operation", {"duration": 1e7, "steps": 1}
        )
        asked: list[Any] = []

        async def decline(request: dict[str, Any]) -> Any:
            asked.append(request.get("params"))
            result = {"action": "decline", "content": None}
            return {"jsonrpc": "2.0", "id": request["id"], "result": result}

        wire.on_request = decline
        trimmed = await call_tool(wire, "trigger-url-elicitation", {"url": "  https://e.org/t \n"})
    assert error_of(comma)["message"] == (
        "MCP error -32602: Resource demo://resource/dynamic/text/1,2 not found"
    )
    assert result_of(completion) == {"completion": {"values": [], "hasMore": False}}
    # a JSON 1e400 reaches the server as Infinity; zod refuses non-finite numbers
    infinite = jsfmt.check({"a": Field("number", required=True)}, {"a": math.inf})[1]
    assert infinite == ["Invalid input: expected number, received Infinity at a"]
    assert text_of(ipv6) == "fetch failed"
    assert text_of(huge).startswith("Long running operation completed. Duration: 10000000 seconds")
    assert asked[0]["url"] == "https://e.org/t"  # zod trimmed it
    assert text_of(trimmed).startswith("❌ User declined to open the URL (Elicitation ID: ")
    assert text_of(trimmed, 1) == '\nRaw result: {\n  "action": "decline"\n}'


async def test_log_level_filters_the_servers_messages(app: FastAPI) -> None:
    async with session(app) as wire:
        await wire.listen()
        await wire.call("logging/setLevel", {"level": "emergency"})
        await wire.call("resources/subscribe", {"uri": "demo://x"})  # its info ack is filtered
        heard = await wire.stop_listening()
    assert heard is not None
    assert not [m for m in heard.messages if isinstance(m, dict) and m.get("method")]


# -- requests the client runs as tasks -------------------------------------------------------------


async def test_async_sampling_times_out_after_sixty_polls(app: FastAPI) -> None:
    task = {"task": {"taskId": "t-1", "status": "working"}}
    replies = {"sampling/createMessage": task, "tasks/get": {"status": "working"}}
    async with session(app, ALL_CAPS, replies) as wire:
        answer = await call_tool(wire, "trigger-sampling-request-async", {"prompt": "p"})
    text = text_of(answer)
    assert text.startswith("[TIMEOUT] Task timed out after 60 poll attempts\n\nProgress:\n")
    assert text.endswith("Poll 60: working")


async def test_async_elicitation_logs_waiting_polls_sparsely(app: FastAPI) -> None:
    states = iter(["input_required"] * 11 + ["completed"])

    async def reply(request: dict[str, Any]) -> Any:
        method = request.get("method")
        if method == "elicitation/create":
            result: Any = {"task": {"taskId": "t-2", "status": "input_required"}}
        elif method == "tasks/get":
            result = {"status": next(states), "statusMessage": "waiting"}
        else:
            result = {"action": "accept", "content": {"name": "Zoe", "agreeToTerms": False}}
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    async with session(app, ALL_CAPS) as wire:
        wire.on_request = reply
        answer = await call_tool(wire, "trigger-elicitation-request-async", {})
    content = answer["result"]["content"]
    assert content[1]["text"] == "User inputs:\n- Name: Zoe\n- Agreed to terms: false"
    progress = content[2]["text"]
    assert "Poll 1: input_required - waiting\nPoll 10: input_required - waiting\n" in progress
    assert "Poll 12: completed - waiting" in progress
    assert "Poll 2:" not in progress


async def test_async_elicitation_timeout_and_refusal(app: FastAPI) -> None:
    replies = {
        "elicitation/create": {"task": {"taskId": "t-3", "status": "working"}},
        "tasks/get": {"status": "working"},
    }
    async with session(app, ALL_CAPS, replies) as wire:
        timed_out = await call_tool(wire, "trigger-elicitation-request-async", {})
        wire.on_request = replier({"elicitation/create": {"error": {"code": -9, "message": "no"}}})
        refused = await call_tool(wire, "trigger-elicitation-request-async", {})
    assert text_of(timed_out).startswith("[TIMEOUT] Task timed out after 600 poll attempts")
    assert text_of(refused) == "MCP error -9: no"


async def test_async_sampling_refused(app: FastAPI) -> None:
    replies = {"sampling/createMessage": {"error": {"code": -5, "message": "busy"}}}
    async with session(app, ALL_CAPS, replies) as wire:
        refused = await call_tool(wire, "trigger-sampling-request-async", {"prompt": "p"})
    assert refused["result"] == {
        "content": [{"type": "text", "text": "MCP error -5: busy"}],
        "isError": True,
    }


async def test_sampling_and_elicitation_reply_checks(app: FastAPI) -> None:
    replies = {
        "sampling/createMessage": {"model": "m", "role": "robot", "content": {}},
        "elicitation/create": {"action": "maybe"},
    }
    async with session(app, ALL_CAPS, replies) as wire:
        sampled = await call_tool(wire, "trigger-sampling-request", {"prompt": "p"})
        elicited = await call_tool(wire, "trigger-elicitation-request", {})
        url = await call_tool(wire, "trigger-url-elicitation", {"url": "https://e.com"})
    assert '"message": "Invalid option: expected one of \\"user\\"|\\"assistant\\""' in text_of(
        sampled
    )
    assert '"values": [\n      "accept",\n      "decline",\n      "cancel"\n    ]' in text_of(
        elicited
    )
    assert text_of(url) == text_of(elicited)


# -- session resources -----------------------------------------------------------------------------


async def test_a_session_file_registered_again_elsewhere_leaves_the_first_session(
    app: FastAPI,
) -> None:
    data = {"name": "shared.gz", "data": "data:,one"}
    async with session(app) as first, session(app) as second:
        await call_tool(first, "gzip-file-as-resource", data)
        await call_tool(second, "gzip-file-as-resource", data)
        gone = await first.call("resources/read", {"uri": "demo://resource/session/shared.gz"})
        kept = await second.call("resources/read", {"uri": "demo://resource/session/shared.gz"})
    assert error_of(gone) == {
        "code": -32602,
        "message": "MCP error -32602: Resource demo://resource/session/shared.gz not found",
    }
    assert isinstance(result_of(kept), dict)


@pytest.mark.parametrize(
    "data",
    ["data:text/plain;base64", "data:;base64,@@@", "https://example.com/file.txt"],
)
async def test_gzip_fetch_failures(app: FastAPI, data: str) -> None:
    async with session(app) as wire:
        answer = await call_tool(wire, "gzip-file-as-resource", {"name": "x.gz", "data": data})
    assert answer["result"] == {
        "content": [{"type": "text", "text": "fetch failed"}],
        "isError": True,
    }


# -- prompts and completions -----------------------------------------------------------------------


async def test_prompt_and_completion_edges(app: FastAPI) -> None:
    async with session(app) as wire:
        nameless = await wire.call("prompts/get", {})
        other_arg = await wire.call(
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "demo://resource/dynamic/text/{resourceId}"},
                "argument": {"name": "other", "value": "1"},
            },
        )
        state = await wire.call(
            "prompts/get", {"name": "args-prompt", "arguments": {"city": "Reno", "state": ""}}
        )
    assert isinstance(error_of(nameless), dict)
    assert error_of(nameless)["code"] == -32603
    assert result_of(other_arg) == {"completion": {"values": [], "hasMore": False}}
    assert isinstance(result_of(state), dict)
    assert result_of(state)["messages"][0]["content"]["text"] == "What's weather in Reno?"


async def test_long_running_operation_without_steps_answers_at_once(app: FastAPI) -> None:
    async with session(app) as wire:
        answer = await call_tool(
            wire,
            "trigger-long-running-operation",
            {"duration": 5, "steps": 0},
            _meta={"progressToken": 1},
        )
    assert text_of(answer) == "Long running operation completed. Duration: 5 seconds, Steps: 0."
