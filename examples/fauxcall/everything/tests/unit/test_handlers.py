"""The computed tools, resources, prompts, completions and tasks, driven like a real client."""

from __future__ import annotations

import base64
import gzip
import json
import math
import random
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import httpx2
import pytest
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything import handlers
from fauxcall_everything.app import build_app
from fauxcall_everything.jsnum import js_number
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from fauxcall.types import JsonObject, JsonValue

BASE = "http://fake.test"
ALL: Any = {
    "sampling": {},
    "elicitation": {"form": {}, "url": {}},
    "roots": {"listChanged": True},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


@pytest.fixture
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


class Session:
    """One connected MCP client of the in-process fake."""

    def __init__(self, wire: McpWireClient, http: httpx2.AsyncClient) -> None:
        self.wire = wire
        self.http = http

    async def call(self, method: str, params: JsonValue = None) -> dict[str, Any]:
        reply = (await self.wire.call(method, params)).response
        assert isinstance(reply, dict)
        return reply

    async def result(self, method: str, params: JsonValue = None) -> Any:
        reply = await self.call(method, params)
        assert "result" in reply, reply
        return reply["result"]

    async def error(self, method: str, params: JsonValue = None) -> dict[str, Any]:
        reply = await self.call(method, params)
        assert "error" in reply, reply
        error: dict[str, Any] = reply["error"]
        return error

    async def tool(self, name: str, arguments: Any = None, **extra: Any) -> Any:
        return await self.result(
            "tools/call", {"name": name, "arguments": arguments or {}, **extra}
        )

    async def text(self, name: str, arguments: JsonObject | None = None) -> str:
        result = await self.tool(name, arguments)
        text: str = result["content"][0]["text"]
        return text

    async def advance(self, seconds: float) -> None:
        response = await self.http.post(
            "/__fauxcall/clock", json={"op": "advance", "seconds": seconds}
        )
        assert response.status_code == 200


@asynccontextmanager
async def connect(app: FastAPI, capabilities: Any = None) -> AsyncIterator[Session]:
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
        wire = McpWireClient(f"{BASE}/mcp", http)
        await wire.initialize(capabilities=capabilities)
        yield Session(wire, http)


def zod(tool: str, detail: str) -> str:
    return f"MCP error -32602: Input validation error: Invalid arguments for tool {tool}: {detail}"


# -- simple tools ------------------------------------------------------------------------------


async def test_echo_and_its_validation(app: FastAPI) -> None:
    async with connect(app) as s:
        assert await s.text("echo", {"message": "hi there"}) == "Echo: hi there"
        missing = await s.tool("echo", {})
        assert missing["isError"] is True
        assert missing["content"][0]["text"] == zod(
            "echo", "Invalid input: expected string, received undefined at message"
        )
        assert await s.text("echo", {"message": 3}) == zod(
            "echo", "Invalid input: expected string, received number at message"
        )


@pytest.mark.parametrize(
    ("a", "b", "text"),
    [
        (1, 2, "The sum of 1 and 2 is 3."),
        (0.1, 0.2, "The sum of 0.1 and 0.2 is 0.30000000000000004."),
        (1e21, 0, "The sum of 1e+21 and 0 is 1e+21."),
        (1.5e-7, 0, "The sum of 1.5e-7 and 0 is 1.5e-7."),
        (0.000001, 0.0000001, "The sum of 0.000001 and 1e-7 is 0.0000011."),
        (1e308, 1e308, "The sum of 1e+308 and 1e+308 is Infinity."),
        (-1e308, -1e308, "The sum of -1e+308 and -1e+308 is -Infinity."),
        (123456789, 987654321, "The sum of 123456789 and 987654321 is 1111111110."),
        (-1.75, -0.25, "The sum of -1.75 and -0.25 is -2."),
    ],
)
async def test_get_sum_formats_numbers_like_javascript(
    app: FastAPI, a: float, b: float, text: str
) -> None:
    async with connect(app) as s:
        assert await s.text("get-sum", {"a": a, "b": b}) == text


async def test_get_sum_reports_every_bad_argument(app: FastAPI) -> None:
    async with connect(app) as s:
        assert await s.text("get-sum", {"a": 10**400, "b": -(10**400)}) == zod(  # recorded: 000390
            "get-sum",
            "Invalid input: expected number, received Infinity at a\n"
            "Invalid input: expected number, received Infinity at b",
        )
        assert await s.text("get-sum", {"b": True}) == zod(
            "get-sum",
            "Invalid input: expected number, received undefined at a\n"
            "Invalid input: expected number, received boolean at b",
        )


async def test_resource_links_alternate_and_respect_bounds(app: FastAPI) -> None:
    async with connect(app) as s:
        default = await s.tool("get-resource-links")
        assert len(default["content"]) == 4
        assert default["content"][1]["uri"] == "demo://resource/dynamic/blob/1"
        assert default["content"][2]["name"] == "Text Resource 2"
        fraction = await s.tool("get-resource-links", {"count": 2.5})
        assert fraction["content"][0]["text"].startswith("Here are 2.5 resource links")
        assert len(fraction["content"]) == 3
        assert await s.text("get-resource-links", {"count": 0}) == zod(
            "get-resource-links", "Too small: expected number to be >=1 at count"
        )
        assert await s.text("get-resource-links", {"count": 11}) == zod(
            "get-resource-links", "Too big: expected number to be <=10 at count"
        )


async def test_resource_reference_embeds_a_dynamic_resource(app: FastAPI) -> None:
    async with connect(app) as s:
        text = await s.tool("get-resource-reference", {"resourceId": 4})
        resource = text["content"][1]["resource"]
        assert resource["uri"] == "demo://resource/dynamic/text/4"
        assert resource["text"].startswith("Resource 4: This is a plaintext resource created at ")
        blob = await s.tool("get-resource-reference", {"resourceType": "Blob", "resourceId": 1e21})
        decoded = base64.b64decode(blob["content"][1]["resource"]["blob"]).decode()
        assert decoded.startswith("Resource 1e+21: This is a base64 blob created at ")
        bad = await s.tool("get-resource-reference", {"resourceId": 0})
        assert bad == {
            "content": [
                {
                    "type": "text",
                    "text": "Invalid resourceId: 0. Must be a finite positive integer.",
                }
            ],
            "isError": True,
        }
        assert await s.text("get-resource-reference", {"resourceType": "Image"}) == zod(
            "get-resource-reference",
            'Invalid option: expected one of "Text"|"Blob" at resourceType',
        )


async def test_structured_content_and_annotations(app: FastAPI) -> None:
    async with connect(app) as s:
        weather = await s.tool("get-structured-content", {"location": "Chicago"})
        assert weather["structuredContent"]["conditions"] == "Light rain / drizzle"
        assert json.loads(weather["content"][0]["text"]) == weather["structuredContent"]
        annotated = await s.tool(
            "get-annotated-message", {"messageType": "debug", "includeImage": True}
        )
        assert annotated["content"][0]["annotations"]["priority"] == 0.3
        assert annotated["content"][1]["mimeType"] == "image/png"
        plain = await s.tool("get-annotated-message", {"messageType": "success"})
        assert len(plain["content"]) == 1


async def test_long_running_operation_reports_progress(app: FastAPI) -> None:
    async with connect(app) as s:
        message: Any = {
            "name": "trigger-long-running-operation",
            "arguments": {"duration": 2, "steps": 4},
            "_meta": {"progressToken": "tok"},
        }
        call = await s.wire.call("tools/call", message)
        notes = [json.loads(data) for _, _, data, _ in call.sse_events[:-1]]
        assert [n["params"]["progress"] for n in notes] == [1, 2, 3, 4]
        assert notes[0]["params"] == {"progress": 1, "total": 4, "progressToken": "tok"}
        result: Any = call.result
        assert result["content"][0]["text"] == (
            "Long running operation completed. Duration: 2 seconds, Steps: 4."
        )
        pauses = list(runtime_of_app(app).pauses)
        assert all(p <= 0.5 for p in pauses[-4:])
        quiet = await s.wire.call(
            "tools/call", {"name": "trigger-long-running-operation", "arguments": {"steps": 0}}
        )
        assert len(quiet.sse_events) == 1
        odd = await s.text("trigger-long-running-operation", {"duration": -1, "steps": 2.5})
        assert odd == "Long running operation completed. Duration: -1 seconds, Steps: 2.5."


def test_timers_behave_like_node() -> None:
    assert handlers._timer_seconds(-5) == 0.001
    assert handlers._timer_seconds(float("nan")) == 0.001
    assert handlers._timer_seconds(3e9) == 0.001
    assert handlers._timer_seconds(250) == 0.25


# -- gzip and session resources ----------------------------------------------------------------


async def test_gzip_creates_a_session_resource(app: FastAPI) -> None:
    async with connect(app) as s:
        link = await s.tool(
            "gzip-file-as-resource",
            {"name": "hi.gz", "data": "data:text/plain;base64,SGVsbG8sIEZhdXhDYWxsIQ=="},
        )
        assert link["content"][0] == {
            "name": "hi.gz",
            "uri": "demo://resource/session/hi.gz",
            "mimeType": "application/gzip",
            "type": "resource_link",
        }
        read = await s.result("resources/read", {"uri": "demo://resource/session/hi.gz"})
        blob = read["contents"][0]["blob"]
        assert blob == "H4sIAAAAAAAAE/NIzcnJ11FwSyytcE7MyVEEAFZX3tsQAAAA"  # recorded
        assert gzip.decompress(base64.b64decode(blob)) == b"Hello, FauxCall!"
        listing = await s.result("resources/list")
        assert listing["resources"][-1] == {
            "uri": "demo://resource/session/hi.gz",
            "name": "hi.gz",
            "mimeType": "application/gzip",
        }
        again = await s.tool(
            "gzip-file-as-resource",
            {"name": "hi.gz", "data": "data:,replaced", "outputType": "resource"},
        )
        inline = again["content"][0]["resource"]
        assert gzip.decompress(base64.b64decode(inline["blob"])) == b"replaced"
        listing = await s.result("resources/list")
        assert [r["uri"] for r in listing["resources"]].count("demo://resource/session/hi.gz") == 1
    async with connect(app) as other:
        error = await other.error("resources/read", {"uri": "demo://resource/session/hi.gz"})
        assert error["message"] == (
            "MCP error -32602: Resource demo://resource/session/hi.gz not found"
        )


async def test_gzip_sources(app: FastAPI) -> None:
    async with connect(app) as s:
        readme = await s.tool("gzip-file-as-resource", {"outputType": "resource"})
        blob = readme["content"][0]["resource"]["blob"]
        assert gzip.decompress(base64.b64decode(blob)) == handlers.DEFAULT_README
        assert await s.text("gzip-file-as-resource", {"data": "https://example.com/x"}) == (
            "fetch failed"
        )
        assert await s.text("gzip-file-as-resource", {"data": "data:text/plain;base64,@@@"}) == (
            "fetch failed"
        )
        assert await s.text("gzip-file-as-resource", {"data": "data:nocomma"}) == "fetch failed"
        assert await s.text("gzip-file-as-resource", {"data": "ftp://example.com/f"}) == (
            "Error processing file ftp://example.com/f: Unsupported URL protocol for "
            "ftp://example.com/f. Only http, https, and data URLs are supported."
        )
        assert await s.text("gzip-file-as-resource", {"data": "not a url"}) == zod(
            "gzip-file-as-resource", "Invalid URL at data"
        )
        spaced = await s.tool("gzip-file-as-resource", {"name": "a b.gz", "data": "data:,x"})
        assert spaced["content"][0]["uri"] == "demo://resource/session/a b.gz"


def test_data_urls() -> None:
    assert handlers.decode_data_url("data:,a%20b") == b"a b"
    assert handlers.decode_data_url("data:;base64,YQ==") == b"a"
    assert handlers.decode_data_url("data:;base64,YQ") == b"a"
    with pytest.raises(handlers.FetchFailedError):
        handlers.decode_data_url("data:;base64,YQ=")
    assert handlers.decode_data_url("data:;base64, Y Q") == b"a"
    with pytest.raises(handlers.FetchFailedError):
        handlers.decode_data_url("data:;base64,Y")


# -- resources, prompts, completions -----------------------------------------------------------


async def test_dynamic_and_static_resources(app: FastAPI) -> None:
    async with connect(app) as s:
        zero_padded = await s.result("resources/read", {"uri": "demo://resource/dynamic/text/01"})
        assert zero_padded["contents"][0]["text"].startswith("Resource 1: ")
        exponent = await s.result("resources/read", {"uri": "demo://resource/dynamic/blob/1e2"})
        text = base64.b64decode(exponent["contents"][0]["blob"]).decode()
        assert text.startswith("Resource 100: This is a base64 blob created at ")
        unknown = await s.error("resources/read", {"uri": "demo://resource/dynamic/text/abc"})
        assert unknown == {
            "code": -32603,
            "message": "Unknown resource: demo://resource/dynamic/text/abc",
        }
        missing = await s.error("resources/read", {"uri": "demo://resource/dynamic/text/"})
        assert missing["code"] == -32602
        static = await s.result(
            "resources/read", {"uri": "demo://resource/static/document/features.md"}
        )
        assert static["contents"][0]["text"].startswith("# Everything Server - Features")
        no_uri = await s.error("resources/read", {})
        assert no_uri["code"] == -32603
        assert json.loads(no_uri["message"])[0]["path"] == ["params", "uri"]


async def test_static_document_without_a_recording(app: FastAPI) -> None:
    runtime = runtime_of_app(app)
    runtime.recordings._by_endpoint = {}
    async with connect(app) as s:
        error = await s.error(
            "resources/read", {"uri": "demo://resource/static/document/features.md"}
        )
        assert error["code"] == -32602


async def test_subscriptions_and_logging_levels(app: FastAPI) -> None:
    async with connect(app) as s:
        assert await s.result("resources/subscribe", {"uri": "demo://x"}) == {}
        assert await s.result("resources/subscribe", {"uri": "demo://x"}) == {}
        assert await s.result("resources/unsubscribe", {"uri": "demo://x"}) == {}
        assert await s.result("resources/unsubscribe", {"uri": "demo://never"}) == {}
        assert (await s.error("resources/subscribe", {}))["code"] == -32603
        assert (await s.error("resources/unsubscribe", {"uri": 5}))["code"] == -32603
        assert await s.result("logging/setLevel", {"level": "error"}) == {}
        bad = await s.error("logging/setLevel", {"level": "loud"})
        assert json.loads(bad["message"])[0]["code"] == "invalid_value"


async def test_prompts(app: FastAPI) -> None:
    async with connect(app) as s:
        simple = await s.result("prompts/get", {"name": "simple-prompt"})
        assert (
            simple["messages"][0]["content"]["text"] == "This is a simple prompt without arguments."
        )
        city = await s.result("prompts/get", {"name": "args-prompt", "arguments": {"city": "Oslo"}})
        assert city["messages"][0]["content"]["text"] == "What's weather in Oslo?"
        both = await s.result(
            "prompts/get", {"name": "args-prompt", "arguments": {"city": "Austin", "state": "TX"}}
        )
        assert both["messages"][0]["content"]["text"] == "What's weather in Austin, TX?"
        team = await s.result(
            "prompts/get",
            {"name": "completable-prompt", "arguments": {"department": "Sales", "name": "Eve"}},
        )
        assert team["messages"][0]["content"]["text"] == (
            "Please promote Eve to the head of the Sales team."
        )
        missing = await s.error("prompts/get", {"name": "completable-prompt"})
        assert missing["message"] == (
            "MCP error -32602: Invalid arguments for prompt completable-prompt: "
            "Invalid input: expected string, received undefined at department\n"
            "Invalid input: expected string, received undefined at name"
        )
        blob = await s.result(
            "prompts/get",
            {"name": "resource-prompt", "arguments": {"resourceType": "Blob", "resourceId": "1e1"}},
        )
        assert (
            blob["messages"][1]["content"]["resource"]["uri"] == "demo://resource/dynamic/blob/10"
        )
        text = await s.result(
            "prompts/get",
            {"name": "resource-prompt", "arguments": {"resourceType": "Text", "resourceId": "2"}},
        )
        assert "id: 2." in text["messages"][0]["content"]["text"]
        bad_type = await s.error(
            "prompts/get",
            {"name": "resource-prompt", "arguments": {"resourceType": "X", "resourceId": "1"}},
        )
        assert bad_type["message"] == "Invalid resourceType: X. Must be Text or Blob."
        bad_id = await s.error(
            "prompts/get",
            {"name": "resource-prompt", "arguments": {"resourceType": "Text", "resourceId": "0"}},
        )
        assert bad_id["message"] == "Invalid resourceId: 0. Must be a finite positive integer."
        unknown = await s.error("prompts/get", {"name": "nope"})
        assert unknown["message"] == "MCP error -32602: Prompt nope not found"
        nameless = await s.error("prompts/get", {})
        assert json.loads(nameless["message"])[0]["path"] == ["params", "name"]


async def complete(s: Session, ref: JsonObject, name: str, value: str, **context: str) -> Any:
    params: JsonObject = {"ref": ref, "argument": {"name": name, "value": value}}
    if context:
        params["context"] = {"arguments": dict(context)}
    return (await s.result("completion/complete", params))["completion"]


async def test_completions(app: FastAPI) -> None:
    team: JsonObject = {"type": "ref/prompt", "name": "completable-prompt"}
    res_prompt: JsonObject = {"type": "ref/prompt", "name": "resource-prompt"}
    template: JsonObject = {
        "type": "ref/resource",
        "uri": "demo://resource/dynamic/text/{resourceId}",
    }
    async with connect(app) as s:
        assert (await complete(s, team, "department", "S"))["values"] == ["Sales", "Support"]
        names = await complete(s, team, "name", "", department="Marketing")
        assert names == {"values": ["Grace", "Henry", "Iris"], "total": 3, "hasMore": False}
        assert (await complete(s, team, "name", ""))["values"] == []
        assert (await complete(s, res_prompt, "resourceType", "B"))["values"] == ["Blob"]
        assert (await complete(s, res_prompt, "resourceId", "7"))["values"] == ["7"]
        assert (await complete(s, template, "resourceId", "x"))["values"] == []
        assert await complete(s, template, "other", "1") == {"values": [], "hasMore": False}
        no_completer = await complete(s, {"type": "ref/prompt", "name": "args-prompt"}, "city", "P")
        assert no_completer == {"values": [], "hasMore": False}
        nope = await s.error(
            "completion/complete",
            {"ref": {"type": "ref/prompt", "name": "nope"}, "argument": {"name": "x", "value": ""}},
        )
        assert nope["message"] == "MCP error -32602: Prompt nope not found"
        bad_template = await s.error(
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "demo://x/{y}"},
                "argument": {"name": "y", "value": ""},
            },
        )
        assert bad_template["message"] == (
            "MCP error -32602: Resource template demo://x/{y} not found"
        )
        odd = await s.error(
            "completion/complete",
            {"ref": {"type": "ref/other"}, "argument": {"name": "x", "value": ""}},
        )
        recorded = json.loads(odd["message"])
        assert recorded[0]["code"] == "invalid_union"
        assert recorded[0]["errors"][1][1]["path"] == ["uri"]
        not_object = await s.error("completion/complete", {"ref": "x"})
        assert json.loads(not_object["message"])[0]["path"] == ["params", "ref"]
        no_argument = await s.error("completion/complete", {"ref": team})
        assert json.loads(no_argument["message"])[0]["path"] == ["params", "argument"]


# -- tasks -----------------------------------------------------------------------------------------


async def test_research_task_lifecycle(app: FastAPI) -> None:
    async with connect(app) as s:
        created = await s.tool("simulate-research-query", {"topic": "tides"}, task={"ttl": 60000})
        task = created["task"]
        assert task["status"] == "working"
        assert task["ttl"] == 300000
        assert task["statusMessage"] == "Gathering sources..."
        await s.advance(1.5)
        got = await s.result("tasks/get", {"taskId": task["taskId"]})
        assert got["statusMessage"] == "Analyzing content..."
        listed = await s.result("tasks/list")
        assert [t["taskId"] for t in listed["tasks"]] == [task["taskId"]]
        assert listed["_meta"] == {}
        result = await s.result("tasks/result", {"taskId": task["taskId"]})
        assert result["content"][0]["text"].startswith("# Research Report: tides\n")
        assert result["_meta"] == {handlers.RELATED_TASK: {"taskId": task["taskId"]}}
        done = await s.result("tasks/get", {"taskId": task["taskId"]})
        assert (done["status"], done["statusMessage"]) == ("completed", "Generating report...")
        terminal = await s.error("tasks/cancel", {"taskId": task["taskId"]})
        assert terminal["message"] == (
            "MCP error -32602: Cannot cancel task in terminal status: completed"
        )
        again = await s.result("tasks/result", {"taskId": task["taskId"]})
        assert again == result
    async with connect(app) as other:
        assert (await other.result("tasks/list"))["tasks"] == []


async def test_research_task_cancel_and_errors(app: FastAPI) -> None:
    async with connect(app) as s:
        created = await s.tool("simulate-research-query", {"topic": "x"}, task={})
        task_id = created["task"]["taskId"]
        cancelled = await s.result("tasks/cancel", {"taskId": task_id})
        assert next(iter(cancelled)) == "_meta"
        assert cancelled["status"] == "cancelled"
        assert cancelled["statusMessage"] == "Client cancelled task execution."
        no_result = await s.error("tasks/result", {"taskId": task_id})
        assert no_result == {"code": -32603, "message": f"Task {task_id} has no result stored"}
        for method, message in [
            ("tasks/get", "MCP error -32602: Failed to retrieve task: Task not found"),
            ("tasks/result", "MCP error -32602: Task not found: nope"),
            ("tasks/cancel", "MCP error -32602: Task not found: nope"),
        ]:
            assert (await s.error(method, {"taskId": "nope"}))["message"] == message
        assert (await s.error("tasks/get", {}))["code"] == -32603
        without = await s.tool("simulate-research-query", {"topic": "x"})
        assert without["isError"] is True
        invalid = await s.error(
            "tools/call",
            {"name": "simulate-research-query", "arguments": {}, "task": {"ttl": 1}},
        )
        assert invalid["message"] == handlers.TASK_CREATION_FAILED
        not_object = await s.error(
            "tools/call", {"name": "echo", "arguments": {"message": "a"}, "task": True}
        )
        assert "expected object, received boolean" in not_object["message"]
        bad_ttl = await s.error(
            "tools/call",
            {"name": "simulate-research-query", "arguments": {"topic": "x"}, "task": {"ttl": "1"}},
        )
        assert json.loads(bad_ttl["message"])[0]["path"] == ["params", "task", "ttl"]


async def test_tasks_expire_after_their_ttl(app: FastAPI) -> None:
    async with connect(app) as s:
        created = await s.tool("simulate-research-query", {"topic": "x"}, task={})
        await s.advance(400)
        assert (await s.result("tasks/list"))["tasks"] == []
        created = await s.tool("simulate-research-query", {"topic": "y"}, task={})
        await s.advance(400)
        error = await s.error("tasks/get", {"taskId": created["task"]["taskId"]})
        assert error["code"] == -32602


async def test_ambiguous_research_waits_for_an_unanswered_elicitation(app: FastAPI) -> None:
    async with connect(app, {"elicitation": {}}) as s:
        created = await s.tool(
            "simulate-research-query", {"topic": "python", "ambiguous": True}, task={}
        )
        task_id = created["task"]["taskId"]
        await s.advance(2.5)
        asking = await s.result("tasks/get", {"taskId": task_id})
        assert asking["status"] == "input_required"
        result = await s.result("tasks/result", {"taskId": task_id})
        report = result["content"][0]["text"]
        assert report.startswith(f"# Research Report: python ({handlers.NO_ELICITATION})")
        assert "**Note:** Elicitation failed" in report


def test_report_matches_the_recorded_text() -> None:
    recorded_tail = (
        "4. Client calls `tasks/result` → Server returns this final result\n\n\n**Key Concepts:**"
    )
    assert recorded_tail in handlers.research_report("x", None)


# -- tools that need client capabilities -----------------------------------------------------------


async def test_capability_tools_are_hidden_without_the_capability(app: FastAPI) -> None:
    async with connect(app) as s:
        names = [t["name"] for t in (await s.result("tools/list"))["tools"]]
        assert "trigger-sampling-request" not in names
        missing = await s.text("trigger-sampling-request", {"prompt": "hi"})
        assert missing == "MCP error -32602: Tool trigger-sampling-request not found"


async def test_capability_tools_time_out_without_a_client_answer(app: FastAPI) -> None:
    async with connect(app, ALL) as s:
        names = [t["name"] for t in (await s.result("tools/list"))["tools"]]
        assert "trigger-sampling-request-async" in names
        for name, arguments in [
            ("trigger-sampling-request", {"prompt": "hi"}),
            ("trigger-sampling-request-async", {"prompt": "hi"}),
            ("trigger-elicitation-request", {}),
            ("trigger-elicitation-request-async", {}),
            ("trigger-url-elicitation", {"url": "https://example.com"}),
        ]:
            result = await s.tool(name, arguments)
            assert result == handlers.text_result(handlers.REQUEST_TIMEOUT_TEXT, error=True)
        for name in ("trigger-sampling-request", "trigger-sampling-request-async"):
            assert await s.text(name, {}) == zod(
                name, "Invalid input: expected string, received undefined at prompt"
            )
        assert await s.text("trigger-url-elicitation", {"url": "nope"}) == zod(
            "trigger-url-elicitation", "Invalid URL at url"
        )
        roots = await s.text("get-roots-list")
        assert roots.startswith("The client supports roots but no roots are currently configured.")
        assert runtime_of_app(app).pauses.count(handlers.ELICITATION_TIMEOUT_S) >= 2


async def test_url_elicitation_error_path(app: FastAPI) -> None:
    async with connect(app, ALL) as s:
        arguments: JsonObject = {"url": "https://example.com/a", "errorPath": True}
        error = await s.error(
            "tools/call", {"name": "trigger-url-elicitation", "arguments": arguments}
        )
        assert error["code"] == -32042
        assert error["message"] == (
            "MCP error -32042: This request requires browser-based authorization."
        )
        elicitation = error["data"]["elicitations"][0]
        assert elicitation["url"] == "https://modelcontextprotocol.io"
        assert len(elicitation["elicitationId"]) == 36
        retry = await s.tool("trigger-url-elicitation", arguments)
        assert retry["content"][0]["text"] == handlers.REQUEST_TIMEOUT_TEXT


# -- session simulations through a stub context ----------------------------------------------------


class Channel:
    """Records pushes; ``pause`` ends the background loop after ``rounds`` waits."""

    def __init__(self, rounds: int = 1) -> None:
        self.pushed: list[tuple[str, JsonValue]] = []
        self.rounds = rounds

    def push(self, method: str, params: JsonValue = None) -> bool:
        self.pushed.append((method, params))
        return True

    async def pause(self, seconds: float) -> None:
        assert seconds == handlers.SIMULATION_PACE_S
        if self.rounds == 0:
            raise _StopLoopError
        self.rounds -= 1


class _StopLoopError(Exception):
    pass


class Context:
    """Just what the simulation handlers use."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.mcp_session = "sess-1"
        self.rng = random.Random(7)
        self.json_body: JsonValue = {"params": {}}
        self.channel = Channel()
        self.work: list[Any] = []

    def push(self, method: str, params: JsonValue = None) -> bool:
        return self.channel.push(method, params)

    def background(self, work: Any) -> Any:
        self.work.append(work)
        return _Task()


class _Task:
    cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


async def _run(work: Any, channel: Channel) -> None:
    with pytest.raises(_StopLoopError):
        await work(channel)


async def test_simulated_logging_respects_the_level() -> None:
    ctx: Any = Context()
    started = await handlers.toggle_simulated_logging(ctx, {})
    assert "for session sess-1 at a 5 second pace" in str(started)
    method, params = ctx.channel.pushed[0]
    assert method == "notifications/message"
    assert isinstance(params, dict)
    assert str(params["data"]).endswith(" - SessionId sess-1")
    loop = Channel(rounds=3)
    await _run(ctx.work[0], loop)
    assert len(loop.pushed) == 3
    handlers.session_state(ctx).level = "emergency"
    quiet = Channel(rounds=20)
    await _run(ctx.work[0], quiet)
    assert all(cast("dict[str, Any]", p[1])["level"] == "emergency" for p in quiet.pushed)
    stopped = await handlers.toggle_simulated_logging(ctx, {})
    assert stopped == handlers.text_result("Stopped simulated logging for session sess-1")


async def test_subscriber_updates_cover_subscriptions() -> None:
    ctx: Any = Context()
    await handlers.resources_subscribe(ctx, {"uri": "demo://a"})
    await handlers.resources_subscribe(ctx, {"uri": "demo://b"})
    await handlers.resources_unsubscribe(ctx, {"uri": "demo://b"})
    logs = [p for m, p in ctx.channel.pushed if m == "notifications/message"]
    assert logs[0] == {
        "level": "info",
        "data": "Received Subscribe Resource request for URI: demo://a from session sess-1",
    }
    ctx.channel.pushed.clear()
    started = await handlers.toggle_subscriber_updates(ctx, {})
    assert "Started simulated resource updated notifications" in str(started)
    assert ctx.channel.pushed == [("notifications/resources/updated", {"uri": "demo://a"})]
    loop = Channel(rounds=2)
    await _run(ctx.work[0], loop)
    assert len(loop.pushed) == 2
    stopped = await handlers.toggle_subscriber_updates(ctx, {})
    assert stopped == handlers.text_result("Stopped simulated resource updates for session sess-1")


async def test_toggles_through_the_fake(app: FastAPI) -> None:
    async with connect(app) as s:
        on = await s.text("toggle-simulated-logging")
        assert on.startswith("Started simulated, random-leveled logging for session ")
        off = await s.text("toggle-simulated-logging")
        assert off.startswith("Stopped simulated logging for session ")
        on = await s.text("toggle-subscriber-updates")
        assert on.startswith("Started simulated resource updated notifications")
        off = await s.text("toggle-subscriber-updates")
        assert off.startswith("Stopped simulated resource updates")


def test_research_timeline() -> None:
    events, clarification = handlers._research_events(ambiguous=False, topic="t")
    task = handlers.ResearchTask("id", "t", 0, events, clarification)
    assert task.state_at(0)[:2] == ("working", "Gathering sources...")
    assert task.state_at(2100)[1] == "Synthesizing findings..."
    assert task.state_at(task.completes_ms)[0] == "completed"
    assert not task.expired(task.completes_ms + handlers.TASK_TTL_MS)
    assert task.expired(task.completes_ms + handlers.TASK_TTL_MS + 1)


# -- after review: behaviour pinned by the round-4 recordings (000385-000427) ----------------------


@pytest.mark.parametrize(
    ("uri", "href"),
    [
        ("demo://resource/dynamic/text/ 1", "demo://resource/dynamic/text/%201"),
        ("DEMO://resource/dynamic/text/../text/1", "demo://resource/dynamic/text/1"),
        ("demo://resource/session/café.gz", "demo://resource/session/caf%C3%A9.gz"),
        ("demo://x/./a/.", "demo://x/a/"),
        ("demo://x/a/..", "demo://x/"),
        ("demo://x?a b#c d", "demo://x?a%20b#c%20d"),
        ("urn:isbn:1", "urn:isbn:1"),
        ("HTTPS://Example.COM/A", "https://example.com/A"),
        ("not a uri", None),
        ("", None),
        ("https:///nohost", None),
    ],
)
def test_normalize_uri(uri: str, href: str | None) -> None:
    assert handlers.normalize_uri(uri) == href


async def test_odd_resource_uris(app: FastAPI) -> None:
    async with connect(app) as s:
        for uri, code, message in [
            ("demo://resource/dynamic/text/%31", -32603, "Unknown resource: "),
            ("demo://resource/dynamic/text/1?x=1", -32603, "Unknown resource: "),
            ("demo://resource/dynamic/text/1_0", -32603, "Unknown resource: "),
            ("demo://resource/dynamic/text/1,2", -32602, "MCP error -32602: Resource "),
            ("not a uri", -32603, "Invalid URL"),
        ]:
            error = await s.error("resources/read", {"uri": uri})
            assert (error["code"], error["message"][: len(message)]) == (code, message)
        upper = await s.result("resources/read", {"uri": "DEMO://resource/dynamic/text/0x10"})
        assert upper["contents"][0]["text"].startswith("Resource 16: ")
        await s.tool("gzip-file-as-resource", {"name": "café.gz", "data": " data:,x "})
        unreadable = await s.error("resources/read", {"uri": "demo://resource/session/café.gz"})
        assert "caf%C3%A9.gz" in unreadable["message"]


async def test_task_param_on_ordinary_tools(app: FastAPI) -> None:
    async with connect(app) as s:
        cases: list[tuple[str, Any]] = [("echo", {"message": "hi"}), ("get-sum", {"a": 1})]
        for name, arguments in cases:
            error = await s.error("tools/call", {"name": name, "arguments": arguments, "task": {}})
            assert error == {"code": -32602, "message": handlers.TASK_CREATION_FAILED}
        await s.error(
            "tools/call", {"name": "toggle-simulated-logging", "arguments": {}, "task": {}}
        )
        flipped = await s.text("toggle-simulated-logging")  # the refused call still toggled it on
        assert flipped.startswith("Stopped simulated logging")


async def test_bad_progress_token_is_refused_by_the_transport(app: FastAPI) -> None:
    async with connect(app) as s:
        call = await s.wire.call(
            "tools/call",
            {"name": "echo", "arguments": {"message": "x"}, "_meta": {"progressToken": {"a": 1}}},
        )
        assert call.status == 400
        assert call.response == {
            "jsonrpc": "2.0",
            "error": {"code": -32700, "message": "Parse error: Invalid JSON-RPC message"},
            "id": None,
        }
        research = await s.wire.call(
            "tools/call",
            {
                "name": "simulate-research-query",
                "arguments": {"topic": "x"},
                "task": {},
                "_meta": {"progressToken": None},
            },
        )
        assert research.status == 400


async def test_tasks_list_pages_by_ten(app: FastAPI) -> None:
    async with connect(app) as s:
        ids = [
            (await s.tool("simulate-research-query", {"topic": str(i)}, task={}))["task"]["taskId"]
            for i in range(11)
        ]
        first = await s.result("tasks/list")
        assert list(first) == ["tasks", "nextCursor", "_meta"]
        assert [t["taskId"] for t in first["tasks"]] == ids[:10]
        assert first["nextCursor"] == ids[9]
        second = await s.result("tasks/list", {"cursor": first["nextCursor"]})
        assert [t["taskId"] for t in second["tasks"]] == ids[10:]
        assert "nextCursor" not in second
        bad = await s.error("tasks/list", {"cursor": "zzz"})
        assert bad["message"] == "MCP error -32602: Failed to list tasks: Invalid cursor: zzz"
        assert (await s.error("tasks/list", {"cursor": 5}))["code"] == -32603


async def test_request_schema_issues(app: FastAPI) -> None:
    async with connect(app) as s:
        city = await s.error("prompts/get", {"name": "args-prompt", "arguments": {"city": 5}})
        assert json.loads(city["message"])[0]["path"] == ["params", "arguments", "city"]
        listed = await s.error("prompts/get", {"name": "args-prompt", "arguments": ["a"]})
        assert json.loads(listed["message"])[0]["expected"] == "record"
        ref: JsonObject = {"type": "ref/prompt", "name": "completable-prompt"}
        value = await s.error(
            "completion/complete", {"ref": ref, "argument": {"name": "department"}}
        )
        assert json.loads(value["message"])[0]["path"] == ["params", "argument", "value"]
        context = await s.error(
            "completion/complete",
            {
                "ref": ref,
                "argument": {"name": "name", "value": ""},
                "context": {"arguments": {"department": 1}},
            },
        )
        assert json.loads(context["message"])[0]["path"][-1] == "department"
        static = await complete(
            s,
            {"type": "ref/resource", "uri": "demo://resource/static/document/features.md"},
            "x",
            "",
        )
        assert static == {"values": [], "hasMore": False}


async def test_capability_gated_tools_follow_registration(app: FastAPI) -> None:
    async def names(capabilities: Any) -> list[str]:
        async with connect(app, capabilities) as s:
            return [t["name"] for t in (await s.result("tools/list"))["tools"]]

    assert (await names({"sampling": {}}))[-2:] == [
        "trigger-sampling-request",
        "simulate-research-query",
    ]
    async_sampling: Any = {
        "sampling": {},
        "tasks": {"requests": {"sampling": {"createMessage": {}}}},
    }
    assert "trigger-sampling-request-async" in await names(async_sampling)
    no_task_requests = {**ALL, "tasks": {"requests": {}}}
    assert not {"trigger-sampling-request-async", "trigger-elicitation-request-async"} & set(
        await names(no_task_requests)
    )


async def test_fast_mode_results_never_date_from_the_future(app: FastAPI) -> None:
    async with connect(app) as s:
        created = await s.tool("simulate-research-query", {"topic": "now"}, task={})
        task_id = created["task"]["taskId"]
        await s.result("tasks/result", {"taskId": task_id})
        got = await s.result("tasks/get", {"taskId": task_id})
        assert got["status"] == "completed"
        assert got["lastUpdatedAt"] >= got["createdAt"]


def test_js_number_is_ascii_only() -> None:
    assert math.isnan(js_number("١٢"))
    assert math.isnan(js_number("0x1_0"))
    assert math.isnan(js_number("0x"))
