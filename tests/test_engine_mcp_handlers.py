"""What custom MCP tool handlers can answer besides a result, and what the fake repeats as recorded:
JSON-RPC errors, whole HTTP replies, per-session state, the caller's name in "unknown" errors, and
the extra members of listing results."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from starlette.responses import Response

from _engine import j
from _mcp import STRUCTURE, asgi_client, mcp_app, mcp_model, rpc_exchange
from fauxcall.build.scaffold import agent_headers, mcp_server_config
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.model import AuthModel, AuthScheme, McpToolModel, McpTransportModel
from fauxcall.model.mcp import PROBE_PROMPT, PROBE_RESOURCE, PROBE_TOOL
from fauxcall.model.realism import LatencyProfile
from fauxcall.runtime import (
    ClientError,
    EarlyResponse,
    RequestContext,
    RpcError,
    register_method,
    register_tool,
)
from fauxcall.runtime.app import runtime_of_app
from fauxcall.runtime.mcp_server import Channel, method_endpoint
from fauxcall.testing.asgi import streaming_client

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonObject, JsonValue

JSON_MODE = McpTransportModel(response_mode="json", issues_session=False, requires_session=False)
DENIED = 'Bearer error="insufficient_scope"'
CREATE: JsonObject = {
    "name": "create_price",
    "inputSchema": {"type": "object", "properties": {"amount": {"type": "integer"}}},
}
LIST: JsonObject = {"name": "list_prices", "inputSchema": {"type": "object"}}


async def _create(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    amount = arguments.get("amount")
    if amount == 0:
        raise RpcError(protocol.INVALID_PARAMS, "amount must be positive", {"param": "amount"})
    if amount == -1:
        raise EarlyResponse(Response(status_code=403, headers={"WWW-Authenticate": DENIED}))
    prices = ctx.state.setdefault("prices", [])
    prices.append(amount)
    return {"content": [{"type": "text", "text": f"price_{len(prices)}"}]}


async def _list(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    del arguments
    return {"content": [{"type": "text", "text": str(ctx.state.get("prices", []))}]}


def _app(tmp_path: Path, errors: dict[str, JsonValue] | None = None, **mcp: Any) -> tuple[Any, Any]:
    tools = [
        McpToolModel(definition=CREATE, behavior="computed"),
        McpToolModel(definition=LIST, behavior="computed"),
        McpToolModel(definition=STRUCTURE, behavior="replay"),
    ]
    model = mcp_model(transport=JSON_MODE, tools=tools, **mcp)
    if errors is not None:
        assert model.mcp is not None
        model.mcp.errors = errors
    app, runtime = mcp_app(tmp_path, model)
    register_tool(app, "create_price", _create)
    register_tool(app, "list_prices", _list)
    return app, runtime


async def _call(
    http: httpx2.AsyncClient, tool: str, arguments: JsonObject, session: str = "default"
) -> httpx2.Response:
    message = protocol.request(1, "tools/call", {"name": tool, "arguments": arguments})
    return await http.post("/mcp", json=message, headers={"X-Fauxcall-Session": session})


async def test_handlers_answer_with_json_rpc_errors(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with asgi_client(app) as http:
        refused = await _call(http, "create_price", {"amount": 0})
    assert refused.status_code == 200
    assert refused.json()["error"] == {
        "code": protocol.INVALID_PARAMS,
        "message": "amount must be positive",
        "data": {"param": "amount"},
    }


async def test_handlers_answer_the_whole_http_request(tmp_path: Path) -> None:
    app, runtime = _app(tmp_path)
    async with asgi_client(app) as http:
        denied = await _call(http, "create_price", {"amount": -1})
    assert denied.status_code == 403
    assert denied.content == b""
    assert denied.headers["www-authenticate"] == DENIED
    (entry,) = runtime.journal.query(path_prefix="/mcp")
    assert entry.status == 403
    assert entry.response_body is None
    assert ("www-authenticate", DENIED) in entry.response_headers


async def test_http_refusals_take_the_tools_error_time(tmp_path: Path) -> None:
    latency = LatencyProfile.from_samples([900.0], errors=[30.0])
    tools = [McpToolModel(definition=CREATE, behavior="computed", latency=latency)]
    app, runtime = mcp_app(
        tmp_path, mcp_model(transport=JSON_MODE, tools=tools), latency=True, fast=False
    )
    register_tool(app, "create_price", _create)
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    runtime._sleep = record_sleep
    async with asgi_client(app) as http:
        assert (await _call(http, "create_price", {"amount": -1})).status_code == 403
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 0.03  # the recorded error time, not the 900 ms of an answer


async def test_state_is_per_session_and_cleared_by_reset(tmp_path: Path) -> None:
    app, runtime = _app(tmp_path)
    async with asgi_client(app) as http:

        async def listed(session: str) -> str:
            reply = await _call(http, "list_prices", {}, session)
            return str(reply.json()["result"]["content"][0]["text"])

        await _call(http, "create_price", {"amount": 500}, "a")
        await _call(http, "create_price", {"amount": 700}, "a")
        await _call(http, "create_price", {"amount": 900}, "b")
        assert await listed("a") == "[500, 700]"
        assert await listed("b") == "[900]"
        runtime.reset("a")
        assert await listed("a") == "[]"
        assert await listed("b") == "[900]"
        runtime.reset()
        assert await listed("b") == "[]"


async def test_unknown_names_are_the_callers_in_the_recorded_errors(tmp_path: Path) -> None:
    def recorded(message: str) -> JsonValue:
        return {"jsonrpc": "2.0", "id": 0, "error": {"code": -32602, "message": message}}

    errors: dict[str, JsonValue] = {
        "unknown_tool": recorded(f"Invalid method parameters: Unknown tool: {PROBE_TOOL}"),
        "resources/read_not_found": recorded(f"Unknown resource: {PROBE_RESOURCE}"),
        "prompts/get_not_found": recorded(f"Prompt {PROBE_PROMPT!r} not found"),
    }
    app, _ = _app(tmp_path, errors=errors)
    async with asgi_client(app) as http:

        async def message(method: str, params: JsonObject) -> str:
            reply = await http.post("/mcp", json=protocol.request(3, method, params))
            body = j(reply.json())
            assert body["id"] == 3
            return str(body["error"]["message"])

        tool = await message("tools/call", {"name": "make_coffee", "arguments": {}})
        assert tool == "Invalid method parameters: Unknown tool: make_coffee"
        resource = await message("resources/read", {"uri": "ui://widget/nope.html"})
        assert resource == "Unknown resource: ui://widget/nope.html"
        assert (
            await message("prompts/get", {"name": "frobnicate"}) == "Prompt 'frobnicate' not found"
        )


async def test_listings_keep_the_real_results_other_members(tmp_path: Path) -> None:
    extras: dict[str, JsonValue] = {
        "tools/list": {"resultType": "tools", "tools": None, "ttlMs": 60000, "_meta": {"v": 1}},
    }
    app, _ = _app(tmp_path, listing_extras=extras)
    async with asgi_client(app) as http:
        tools = j((await http.post("/mcp", json=protocol.request(1, "tools/list"))).json())
        prompts = j((await http.post("/mcp", json=protocol.request(2, "prompts/list"))).json())
    assert list(tools["result"]) == ["resultType", "tools", "ttlMs", "_meta"]  # recorded order
    assert [t["name"] for t in tools["result"]["tools"]] == [
        "create_price",
        "list_prices",
        "read_wiki_structure",
    ]
    assert tools["result"]["ttlMs"] == 60000
    assert prompts["result"] == {"prompts": []}  # nothing recorded beyond the list


async def test_legacy_sse_transport_answers_the_post_with_the_http_reply(tmp_path: Path) -> None:
    transport = McpTransportModel(kind="sse", path="/sse", messages_path="/messages/")
    tools = [McpToolModel(definition=CREATE, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport, tools=tools))
    register_tool(app, "create_price", _create)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/sse", http, transport="sse")
        await wire.initialize()
        refused = await wire.post(
            protocol.request(5, "tools/call", {"name": "create_price", "arguments": {"amount": -1}})
        )
        await wire.close()
    assert refused.status == 403
    assert refused.header("www-authenticate") == DENIED


def test_agent_headers_and_mcp_config_for_each_scheme() -> None:
    model = mcp_model(
        auth=AuthModel(
            schemes=[
                AuthScheme(kind="basic"),
                AuthScheme(kind="bearer"),  # Authorization is taken already
                AuthScheme(kind="api_key", location="header", name="X-Key"),
                AuthScheme(kind="api_key", location="query", name="key"),  # not a header
            ]
        )
    )
    assert agent_headers(model) == {"Authorization": "Basic YW55OnRva2Vu", "X-Key": "any-token"}
    assert mcp_server_config(model, 8701) == {
        "type": "http",
        "url": "http://127.0.0.1:8701/mcp",
        "headers": {"Authorization": "Basic YW55OnRva2Vu", "X-Key": "any-token"},
    }


SSE_MODE = McpTransportModel(response_mode="sse", issues_session=False, requires_session=False)


async def test_methods_can_be_answered_by_code_or_left_to_the_recordings(tmp_path: Path) -> None:
    static = "file:///readme"
    recorded = rpc_exchange(
        "",
        method_endpoint("resources/read", static),
        "resources/read",
        {"uri": static},
        {"jsonrpc": "2.0", "id": 7, "result": {"contents": [{"uri": static, "text": "hi"}]}},
    )
    app, _ = mcp_app(tmp_path, mcp_model(transport=JSON_MODE, tools=[]), recorded, latency=False)

    async def read(ctx: RequestContext, params: JsonObject) -> JsonValue:
        uri = str(params.get("uri"))
        if uri.startswith("demo://dynamic/"):
            return {"contents": [{"uri": uri, "text": f"item {uri.rsplit('/', 1)[-1]}"}]}
        if uri == "demo://broken":
            raise RpcError(-32603, f"Unknown resource: {uri}")
        if uri == "demo://bug":
            raise KeyError("oops")
        return None  # the recordings answer the rest

    register_method(app, "resources/read", read)
    async with asgi_client(app) as http:

        async def call(params: JsonObject) -> Any:
            return j(
                (await http.post("/mcp", json=protocol.request(1, "resources/read", params))).json()
            )

        dynamic = await call({"uri": "demo://dynamic/42"})
        assert dynamic["result"]["contents"][0]["text"] == "item 42"
        assert (await call({"uri": static}))["result"]["contents"][0]["text"] == "hi"
        assert (await call({"uri": "demo://broken"}))["error"]["message"] == (
            "Unknown resource: demo://broken"
        )
        crashed = await http.post(
            "/mcp", json=protocol.request(1, "resources/read", {"uri": "demo://bug"})
        )
        assert crashed.status_code == 200  # never a bare HTTP 500
        assert j(crashed.json())["error"] == {"code": -32603, "message": "'oops'"}
        missing = await call({"uri": "demo://nothing"})
        assert missing["error"]["code"] == protocol.INVALID_PARAMS


async def test_crashing_tools_answer_with_an_internal_error(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def crash(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        raise ZeroDivisionError

    register_tool(app, "create_price", crash)
    async with asgi_client(app) as http:
        reply = await _call(http, "create_price", {"amount": 1})
    assert reply.status_code == 200
    assert reply.json()["error"] == {"code": -32603, "message": "Internal error"}


async def test_protocol_defaults_for_methods_nothing_recorded(tmp_path: Path) -> None:
    capabilities: JsonValue = {
        "tools": {},
        "resources": {"subscribe": True},
        "completions": {},
        "logging": {},
    }
    model = mcp_model(transport=JSON_MODE, tools=[])
    assert model.mcp is not None
    model.mcp.initialize_result = {"protocolVersion": "2025-06-18", "capabilities": capabilities}
    app, _ = mcp_app(tmp_path, model)
    async with asgi_client(app) as http:

        async def call(method: str, params: JsonObject) -> Any:
            return j((await http.post("/mcp", json=protocol.request(1, method, params))).json())

        assert (await call("resources/subscribe", {"uri": "x://1"}))["result"] == {}
        assert (await call("resources/unsubscribe", {"uri": "x://1"}))["result"] == {}
        assert (await call("logging/setLevel", {"level": "debug"}))["result"] == {}
        completion = await call(
            "completion/complete", {"ref": {"type": "ref/prompt", "name": "p"}, "argument": {}}
        )
        assert completion["result"] == {"completion": {"values": [], "hasMore": False}}
        assert (await call("tasks/list", {}))["error"]["code"] == protocol.METHOD_NOT_FOUND
    plain = mcp_model(transport=JSON_MODE, tools=[])  # no subscribe, no completions
    app, _ = mcp_app(tmp_path / "plain", plain)
    async with asgi_client(app) as http:
        for method in ("resources/subscribe", "completion/complete"):
            reply = j((await http.post("/mcp", json=protocol.request(1, method, {}))).json())
            assert reply["error"]["code"] == protocol.METHOD_NOT_FOUND


async def test_notifications_stream_before_the_answer(tmp_path: Path) -> None:
    long_task: JsonObject = {"name": "long", "inputSchema": {"type": "object"}}
    tools = [McpToolModel(definition=long_task, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=SSE_MODE, tools=tools))

    async def long(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        for step in range(1, 3):
            ctx.progress(step, 2, f"step {step}")
            await asyncio.sleep(0.05)
        ctx.notify("notifications/message", {"level": "info", "data": "done"})
        return {"content": [{"type": "text", "text": "finished"}]}

    async def quick(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.progress(1)  # no progress token: nothing is sent
        return {"content": []}

    register_tool(app, "long", long)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        params: JsonObject = {"name": "long", "arguments": {}, "_meta": {"progressToken": "t-1"}}
        call = await wire.call("tools/call", params)
        events = [json.loads(data) for _, _, data, _ in call.sse_events]
        assert [e.get("method") for e in events] == [
            "notifications/progress",
            "notifications/progress",
            "notifications/message",
            None,  # the answer comes last
        ]
        assert events[0]["params"] == {
            "progressToken": "t-1",
            "progress": 1,
            "total": 2,
            "message": "step 1",
        }
        times = [t for t, _, _, _ in call.sse_events]
        assert times[1] - times[0] >= 30  # streamed as they happened, not all at the end
        assert j(call.result)["content"][0]["text"] == "finished"
        register_tool(app, "long", quick)
        plain = await wire.call("tools/call", {"name": "long", "arguments": {}})
        assert [json.loads(d).get("method") for _, _, d, _ in plain.sse_events] == [None]


async def test_arguments_that_arent_an_object_are_not_made_one(tmp_path: Path) -> None:
    recorded = rpc_exchange(
        "",
        "MCP tools/call:read_wiki_structure",
        "tools/call",
        {"name": "read_wiki_structure", "arguments": None},
        {"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "Expected object"}},
    )
    app, _ = mcp_app(tmp_path, mcp_model(transport=JSON_MODE), recorded)
    async with asgi_client(app) as http:

        async def call(arguments: JsonValue) -> Any:
            params: JsonValue = {"name": "read_wiki_structure", "arguments": arguments}
            return j(
                (await http.post("/mcp", json=protocol.request(1, "tools/call", params))).json()
            )

        assert (await call(None))["error"] == {"code": -32603, "message": "Expected object"}
        assert (await call(["a"]))["error"] == {
            "code": protocol.INVALID_PARAMS,
            "message": "Invalid params: arguments must be an object",
        }
        no_arguments = protocol.request(1, "tools/call", {"name": "read_wiki_structure"})
        reply = j((await http.post("/mcp", json=no_arguments)).json())
        assert reply["result"]["isError"] is True  # no arguments are {}: refused by the schema


@pytest.mark.parametrize("fast", [True, False])
async def test_handlers_take_real_time_except_in_fast_mode(tmp_path: Path, fast: bool) -> None:
    app, runtime = _app(tmp_path)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    runtime._sleep = sleep  # stands in for the clock here; fakes' tests read runtime.pauses
    runtime.settings.fast = fast

    async def slow(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        await ctx.pause(0.25)
        await ctx.pause(0.5)
        return {"content": []}

    register_tool(app, "create_price", slow)
    async with asgi_client(app) as http:
        await _call(http, "create_price", {"amount": 1})
    assert list(runtime.pauses) == [0.25, 0.5]  # noted either way, for tests to check
    assert slept == ([] if fast else [0.25, 0.5])


async def test_streaming_edge_cases(tmp_path: Path) -> None:
    tools = [
        McpToolModel(
            definition={"name": name, "inputSchema": {"type": "object"}}, behavior="computed"
        )
        for name in ("burst", "late_refusal", "quiet_json")
    ]
    app, _ = mcp_app(tmp_path, mcp_model(transport=SSE_MODE, tools=tools))

    async def burst(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        for n in range(3):  # all before the answer, without waiting
            ctx.notify("notifications/message", {"level": "info", "data": n})
        return {"content": []}

    async def late_refusal(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.notify("notifications/message", {"level": "info", "data": "started"})
        await asyncio.sleep(0.01)
        ctx.notify("notifications/message", {"level": "info", "data": "still going"})
        ctx.notify("notifications/message", {"level": "info", "data": "and more"})
        await asyncio.sleep(0.01)
        raise EarlyResponse(Response(status_code=403))

    register_tool(app, "burst", burst)
    register_tool(app, "late_refusal", late_refusal)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        fast = await wire.call("tools/call", {"name": "burst", "arguments": {}})
        data = [json.loads(d) for _, _, d, _ in fast.sse_events]
        assert [m.get("params", {}).get("data") for m in data] == [0, 1, 2, None]
        late = await wire.call("tools/call", {"name": "late_refusal", "arguments": {}})
        assert late.status == 200  # the stream had started: too late for an HTTP 403
        assert j(late.error) == {"code": -32603, "message": "refused"}
    json_app, _ = mcp_app(tmp_path / "json", mcp_model(transport=JSON_MODE, tools=tools))
    register_tool(json_app, "quiet_json", burst)
    async with asgi_client(json_app) as http:
        reply = await http.post(
            "/mcp", json=protocol.request(1, "tools/call", {"name": "quiet_json", "arguments": {}})
        )
    assert reply.json()["result"] == {"content": []}  # plain JSON can't carry notifications


async def test_the_legacy_sse_transport_streams_notifications_too(tmp_path: Path) -> None:
    transport = McpTransportModel(kind="sse", path="/sse", messages_path="/messages/")
    tools = [McpToolModel(definition=CREATE, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport, tools=tools))

    async def chatty(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.notify("notifications/message", {"level": "info", "data": "hello"})
        return {"content": []}

    register_tool(app, "create_price", chatty)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/sse", http, transport="sse")
        await wire.initialize()
        call = await wire.call("tools/call", {"name": "create_price", "arguments": {}})
        await wire.close()
    assert [json.loads(d).get("method") for _, _, d, _ in call.sse_events][:1] == [
        "notifications/message"
    ]


def test_register_method_needs_an_mcp_fake(tmp_path: Path) -> None:
    from fauxcall.config import FakeSettings  # noqa: PLC0415
    from fauxcall.runtime import FakeRuntime, create_app  # noqa: PLC0415

    model = mcp_model()
    model = model.model_copy(update={"kind": "rest", "mcp": None})
    settings = FakeSettings(data_dir=tmp_path, log_level="WARNING")
    app = create_app(settings, runtime=FakeRuntime(settings, model), configure_logs=False)

    async def handler(ctx: RequestContext, params: JsonObject) -> JsonValue:
        return None

    with pytest.raises(ValueError, match="no MCP server"):
        register_method(app, "resources/read", handler)


def test_wire_helpers() -> None:
    from fauxcall.model.recording import Body, CapturedRequest, Exchange  # noqa: PLC0415
    from fauxcall.runtime.mcp_server import _declared, _sent_arguments  # noqa: PLC0415
    from fauxcall.types import HttpMethod  # noqa: PLC0415

    init: JsonValue = {"method": "initialize", "params": {"capabilities": {"sampling": {}}}}
    assert _declared([{"method": "ping"}, init, "odd"]) == {"sampling": {}}
    assert _declared([{"method": "initialize", "params": {"capabilities": []}}]) == {}
    sent = Exchange(
        id="1",
        endpoint="MCP tools/call:x",
        started_at=datetime.now(UTC),
        started_ns=0,
        request=CapturedRequest(
            method=HttpMethod.POST,
            url="http://x/mcp",
            path="/mcp",
            body=Body(kind="json", json_value={"method": "tools/call", "params": {"name": "x"}}),
        ),
    )
    assert _sent_arguments(sent) is None  # no arguments were sent


def test_tools_can_require_sets_of_capabilities() -> None:
    url_only = McpToolModel.model_validate(
        {"definition": {"name": "a"}, "requires": ["elicitation.url"]}
    )
    assert url_only.requires == [["elicitation.url"]]  # the short form: any one of them
    assert url_only.offered_to({"elicitation": {"url": {}}})
    assert not url_only.offered_to({"elicitation": {}})
    both = McpToolModel(definition={"name": "b"}, requires=[["sampling", "tasks.requests"]])
    assert both.offered_to({"sampling": {}, "tasks": {"requests": {}}})
    assert not both.offered_to({"sampling": {}})
    assert McpToolModel(definition={"name": "c"}).offered_to({})


async def test_listings_and_capabilities_reach_handlers(tmp_path: Path) -> None:
    sessions = McpTransportModel(response_mode="sse")  # capabilities live in the MCP session
    app, _ = mcp_app(tmp_path, mcp_model(transport=sessions, tools=[]))
    seen: list[JsonValue] = []

    async def resources(ctx: RequestContext, params: JsonObject) -> JsonValue:
        seen.append(ctx.client_capabilities)
        made = ctx.state.get("made", [])
        if not made:
            return None  # nothing created yet: the recorded listing
        return {"resources": [*ctx.model.mcp.resources, *made]}  # type: ignore[union-attr]

    async def create(ctx: RequestContext, params: JsonObject) -> JsonValue:
        ctx.state.setdefault("made", []).append({"uri": "session://1", "name": "made"})
        return {}

    register_method(app, "resources/list", resources)
    register_method(app, "custom/create", create)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize(capabilities={"sampling": {}})
        assert j((await wire.call("resources/list")).result) == {"resources": []}
        await wire.call("custom/create")
        listed = j((await wire.call("resources/list")).result)["resources"]
        assert listed == [{"uri": "session://1", "name": "made"}]
        await wire.close()
    assert seen[0] == {"sampling": {}}


async def test_malformed_calls_get_the_recorded_answers(tmp_path: Path) -> None:
    def refused(message: str) -> JsonValue:
        return {"jsonrpc": "2.0", "id": 0, "error": {"code": -32603, "message": message}}

    model = mcp_model(transport=McpTransportModel(response_mode="json", reinitialize_status=400))
    assert model.mcp is not None
    model.mcp.errors |= {
        "tool_call_without_name": refused("name: Required"),
        "arguments_not_object": refused("read_wiki_structure: arguments must be an object"),
        "second_initialize": refused("Invalid Request: Server already initialized"),
    }
    model.mcp.arguments_probe_tool = "read_wiki_structure"
    app, _ = mcp_app(tmp_path, model)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        nameless = await wire.call("tools/call", {"arguments": {}})
        assert j(nameless.error) == {"code": -32603, "message": "name: Required"}
        odd = await wire.call("tools/call", {"name": "ask_question", "arguments": "x"})
        assert j(odd.error)["message"] == "ask_question: arguments must be an object"
        again = await wire.post(protocol.request(9, "initialize", {"capabilities": {}}))
        assert again.status == 400
        assert j(again.response) == {
            "jsonrpc": "2.0",
            "id": 9,
            "error": {"code": -32603, "message": "Invalid Request: Server already initialized"},
        }
        await wire.close()
    plain, _ = mcp_app(tmp_path / "plain", mcp_model(transport=JSON_MODE))
    async with asgi_client(plain) as http:
        reply = j((await http.post("/mcp", json=protocol.request(1, "tools/call", {}))).json())
    assert reply["error"] == {"code": -32602, "message": "Invalid params: name is required"}


async def test_pushes_reach_the_clients_open_stream(tmp_path: Path) -> None:
    transport = McpTransportModel(response_mode="json", get_stream="sse")
    toggle: JsonObject = {"name": "toggle", "inputSchema": {"type": "object"}}
    app, _ = mcp_app(
        tmp_path,
        mcp_model(
            transport=transport, tools=[McpToolModel(definition=toggle, behavior="computed")]
        ),
    )
    started: list[asyncio.Task[None] | None] = []

    async def ticking(channel: Channel) -> None:
        for n in range(2):
            await channel.pause(0.01)
            channel.push("notifications/message", {"level": "info", "data": f"tick {n}"})
        await channel.pause(60)  # still running when the session ends

    async def on(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        assert ctx.push("notifications/resources/list_changed")
        started.append(ctx.background(ticking))
        return {"content": []}

    register_tool(app, "toggle", on)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        assert wire.session_id is not None
        headers = {"mcp-session-id": wire.session_id, "accept": "text/event-stream"}
        async with http.stream("GET", "/mcp", headers=headers) as stream:
            lines = stream.aiter_lines()
            await wire.call("tools/call", {"name": "toggle", "arguments": {}})
            received: list[str] = []
            while len(received) < 3:
                line = await anext(lines)
                if line.startswith("data:"):
                    received.append(json.loads(line[5:])["method"])
        assert received == [
            "notifications/resources/list_changed",
            "notifications/message",
            "notifications/message",
        ]
        await wire.close()  # DELETE ends the session and what it left running
    (task,) = started
    assert task is not None
    await asyncio.sleep(0)
    assert task.cancelled()


async def test_without_an_open_stream_pushes_are_dropped(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    pushed: list[bool] = []

    async def lonely(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        pushed.append(ctx.push("notifications/message"))
        return {"content": []}

    register_tool(app, "create_price", lonely)
    async with asgi_client(app) as http:
        await _call(http, "create_price", {"amount": 1})
    assert pushed == [False]
    context = RequestContext.__new__(RequestContext)
    context.channel = None
    assert context.push("x") is False
    assert context.background(ticking_never) is None


async def ticking_never(channel: Channel) -> None:  # pragma: no cover - never started
    del channel


async def test_the_official_sdk_answers_the_fakes_requests(tmp_path: Path) -> None:
    from mcp import Client, types  # noqa: PLC0415
    from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415

    ask: JsonObject = {"name": "ask_the_model", "inputSchema": {"type": "object"}}
    roots: JsonObject = {"name": "list_roots", "inputSchema": {"type": "object"}}
    tools = [
        McpToolModel(definition=ask, behavior="computed", requires=[["sampling"]]),
        McpToolModel(definition=roots, behavior="computed"),
    ]
    app, _ = mcp_app(tmp_path, mcp_model(transport=McpTransportModel(), tools=tools))

    async def ask_the_model(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        answer = await ctx.ask_client(
            "sampling/createMessage",
            {
                "messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}],
                "maxTokens": 9,
            },
        )
        text = answer["content"]["text"]  # type: ignore[index, call-overload]
        return {"content": [{"type": "text", "text": f"the model said: {text}"}]}

    async def list_roots(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        answer = await ctx.ask_client("roots/list", within=0.2)  # a client that never answers
        return {"content": [{"type": "text", "text": str(answer)}]}

    register_tool(app, "ask_the_model", ask_the_model)
    register_tool(app, "list_roots", list_roots)

    async def sample(
        context: Any, params: types.CreateMessageRequestParams
    ) -> types.CreateMessageResult:
        return types.CreateMessageResult(
            role="assistant", content=types.TextContent(type="text", text="hello"), model="m"
        )

    async with (
        streaming_client(app) as http,
        Client(
            streamable_http_client("http://fake.test/mcp", http_client=http),
            sampling_callback=sample,
        ) as client,
    ):
        result = await client.call_tool("ask_the_model", {})
        assert j(result.content[0]).text == "the model said: hello"
        with pytest.raises(MCPError, match="List roots not supported"):  # the client refused
            await client.call_tool("list_roots", {})


async def test_a_client_that_never_answers_times_out(tmp_path: Path) -> None:
    roots: JsonObject = {"name": "list_roots", "inputSchema": {"type": "object"}}
    tools = [McpToolModel(definition=roots, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=McpTransportModel(), tools=tools))

    async def list_roots(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        return await ctx.ask_client("roots/list", within=0.05)

    async def on_get_stream(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        return await ctx.channel.ask("roots/list", within=0.05)  # type: ignore[union-attr]

    register_tool(app, "list_roots", list_roots)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)  # reads the request, never answers
        await wire.initialize()
        silent = await wire.call("tools/call", {"name": "list_roots", "arguments": {}})
        assert j(silent.error) == {"code": -32001, "message": "Request timed out"}
        asked = next(json.loads(d) for _, _, d, _ in silent.sse_events)
        assert (asked["method"], asked["id"]) == ("roots/list", "fauxcall-1")
        register_tool(app, "list_roots", on_get_stream)  # no GET stream open: unreachable
        unreachable = await wire.call("tools/call", {"name": "list_roots", "arguments": {}})
        assert j(unreachable.error)["code"] == -32001
        await wire.close()


async def test_client_errors_and_odd_answers(tmp_path: Path) -> None:
    sessions = McpTransportModel(response_mode="json", get_stream="sse")
    definition: JsonObject = {"name": "ask", "inputSchema": {"type": "object"}}
    tools = [McpToolModel(definition=definition, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=sessions, tools=tools))
    asked: list[JsonValue] = []

    async def ask(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        try:
            return await ctx.ask_client("elicitation/create", {"message": "?"}, within=5)
        except ClientError as error:
            return {"content": [{"type": "text", "text": f"{error.code} {error.message}"}]}

    register_tool(app, "ask", ask)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {"mcp-session-id": str(wire.session_id), "accept": "text/event-stream"}
        async with http.stream("GET", "/mcp", headers=headers) as stream:
            lines = stream.aiter_lines()
            call = asyncio.ensure_future(wire.call("tools/call", {"name": "ask", "arguments": {}}))
            while True:  # JSON mode: the request arrives on the GET stream
                line = await anext(lines)
                if line.startswith("data:"):
                    asked.append(json.loads(line[5:]))
                    break
            request_id = asked[0]["id"]  # type: ignore[index, call-overload]
            await wire.post({"jsonrpc": "2.0", "id": "unknown", "result": {}})  # nobody waits
            refusal: JsonValue = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -1, "message": "no"},
            }
            await wire.post(refusal)
            answered = await call
        assert j(answered.result)["content"][0]["text"] == "-1 no"
        await wire.close()


async def test_client_notifications_reach_their_handlers(tmp_path: Path) -> None:
    sessions = McpTransportModel(response_mode="json", get_stream="sse")
    app, _ = mcp_app(tmp_path, mcp_model(transport=sessions))
    roots: list[JsonValue] = []
    done = asyncio.Event()

    async def roots_changed(ctx: RequestContext, params: JsonObject) -> JsonValue:
        roots.append(await ctx.ask_client("roots/list", within=5))  # as the real server does
        done.set()
        return None

    async def broken(ctx: RequestContext, params: JsonObject) -> JsonValue:
        raise RuntimeError("a bug in custom code")

    register_method(app, "notifications/roots/list_changed", roots_changed)
    register_method(app, "notifications/broken", broken)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {"mcp-session-id": str(wire.session_id), "accept": "text/event-stream"}
        async with http.stream("GET", "/mcp", headers=headers) as stream:
            lines = stream.aiter_lines()
            broke = await wire.post({"jsonrpc": "2.0", "method": "notifications/broken"})
            told = await wire.post({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"})
            assert (broke.status, told.status) == (202, 202)  # at once, before any answer
            asked: dict[str, JsonValue] = {}
            while not asked:  # the question arrives on the GET stream
                line = await anext(lines)
                if line.startswith("data:"):
                    asked = json.loads(line[5:])
            assert asked["method"] == "roots/list"
            listed: JsonValue = {"roots": [{"uri": "file:///work"}]}
            await wire.post({"jsonrpc": "2.0", "id": asked["id"], "result": listed})
            await asyncio.wait_for(done.wait(), 5)
        assert roots == [listed]
        await wire.close()


async def test_ask_client_edges() -> None:
    from starlette.requests import Request as StarletteRequest  # noqa: PLC0415

    from fauxcall.mcpwire.client import McpWireClient as Wire  # noqa: PLC0415
    from fauxcall.runtime.mcp_server import _legacy_session  # noqa: PLC0415

    context = RequestContext.__new__(RequestContext)
    context.channel = None  # not an MCP call: there is no client to ask
    with pytest.raises(ClientError, match="Request timed out"):
        await context.ask_client("roots/list")
    scope = {"type": "http", "query_string": b"session_id=not-hex", "headers": []}
    assert _legacy_session(StarletteRequest(scope)) is None
    wire = Wire("http://x/mcp", httpx2.AsyncClient())
    asked: list[JsonValue] = []

    async def never(request: JsonObject) -> JsonValue:
        asked.append(request)
        return None  # leave it unanswered

    wire.on_request = never
    await wire._answer_server("not json")
    await wire._answer_server('{"jsonrpc": "2.0", "method": "notifications/x"}')  # no id
    await wire._answer_server('{"jsonrpc": "2.0", "id": 1, "method": "roots/list"}')
    assert asked == [{"jsonrpc": "2.0", "id": 1, "method": "roots/list"}]
    await wire.http.aclose()


async def test_long_calls_keep_their_stream_alive(tmp_path: Path) -> None:
    alive = McpTransportModel(
        response_mode="sse", keep_alive=": keepalive", keep_alive_seconds=0.02
    )
    definition: JsonObject = {"name": "slow", "inputSchema": {"type": "object"}}
    tools = [McpToolModel(definition=definition, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=alive, tools=tools))

    async def slow(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        await asyncio.sleep(0.1)  # idle long enough for a keep-alive before anything is sent
        ctx.notify("notifications/message", {"level": "info", "data": "halfway"})
        await asyncio.sleep(0.1)  # and again between the notification and the answer
        return {"content": []}

    register_tool(app, "slow", slow)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {
            "mcp-session-id": str(wire.session_id),
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        call = protocol.request(7, "tools/call", {"name": "slow", "arguments": {}})
        response = await http.post("/mcp", json=call, headers=headers)
        frames = [frame for frame in response.text.split("\n\n") if frame]
        assert frames[0] == ": keepalive"  # the stream opened on the first keep-alive
        data = [json.loads(f.split("data: ", 1)[1]) for f in frames if "data: " in f]
        assert [m.get("method") or m["id"] for m in data] == ["notifications/message", 7]
        between = frames[frames.index(next(f for f in frames if "halfway" in f)) + 1]
        assert between == ": keepalive"
        await wire.close()


async def test_no_stream_to_watch(tmp_path: Path) -> None:
    refused = McpTransportModel(get_stream="status")
    app, _ = mcp_app(tmp_path, mcp_model(transport=refused))
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        assert await wire.watch_stream(0.1) == (None, None)
        await wire.close()


async def _events(stream: httpx2.Response, count: int) -> list[tuple[str, JsonValue]]:
    """The next ``count`` events on an SSE stream: (id, message)."""
    found: list[tuple[str, JsonValue]] = []
    event_id = ""
    lines = stream.aiter_lines()
    while len(found) < count:
        async with asyncio.timeout(5):  # a missing event fails the test instead of hanging it
            line = await anext(lines)
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("data:") and line[5:].strip():
            found.append((event_id, json.loads(line[5:])))
    return found


async def test_a_reconnecting_client_gets_what_it_missed(tmp_path: Path) -> None:
    numbered = McpTransportModel(response_mode="json", get_stream="sse", sse_event_ids=True)
    definition: JsonObject = {"name": "say", "inputSchema": {"type": "object"}}
    tools = [McpToolModel(definition=definition, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=numbered, tools=tools))

    async def say(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.push("notifications/message", {"level": "info", "data": arguments["text"]})
        return {"content": []}

    register_tool(app, "say", say)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        headers = {"mcp-session-id": str(wire.session_id), "accept": "text/event-stream"}
        async with http.stream("GET", "/mcp", headers=headers) as stream:
            await wire.call("tools/call", {"name": "say", "arguments": {"text": "one"}})
            [(first_id, _)] = await _events(stream, 1)
        for text in ("two", "three"):  # no stream open: kept, not dropped
            await wire.call("tools/call", {"name": "say", "arguments": {"text": text}})
        resumed = {**headers, "last-event-id": first_id}
        async with http.stream("GET", "/mcp", headers=resumed) as stream:
            missed = await _events(stream, 2)
        assert [j(m)["params"]["data"] for _, m in missed] == ["two", "three"]
        async with http.stream("GET", "/mcp", headers={**headers, "last-event-id": missed[0][0]}):
            pass  # the replayed events kept their ids: resuming from them works too
        unknown = {**headers, "last-event-id": "never-sent"}
        async with http.stream("GET", "/mcp", headers=unknown) as stream:
            assert stream.status_code == 200  # nothing to replay: just the stream
        await wire.close()


async def test_resuming_a_calls_stream(tmp_path: Path) -> None:
    numbered = McpTransportModel(
        response_mode="sse",
        get_stream="sse",
        sse_event_ids=True,
        priming_versions=["2025-11-25"],
        protocol_versions={v: v for v in protocol.KNOWN_VERSIONS},  # so 2025-11-25 is spoken
    )
    definition: JsonObject = {"name": "slow", "inputSchema": {"type": "object"}}
    tools = [McpToolModel(definition=definition, behavior="computed")]
    app, _ = mcp_app(tmp_path, mcp_model(transport=numbered, tools=tools))

    async def slow(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        ctx.notify("notifications/message", {"level": "info", "data": "halfway"})
        await asyncio.sleep(0.05)
        return {"content": [{"type": "text", "text": "done"}]}

    register_tool(app, "slow", slow)
    async with streaming_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http, protocol_version="2025-11-25")
        await wire.initialize()
        answered = await wire.call("tools/call", {"name": "slow", "arguments": {}})
        priming, halfway = answered.sse_events[0][3], answered.sse_events[1][3]
        headers = {"mcp-session-id": str(wire.session_id), "accept": "text/event-stream"}
        for resume_from, expected in ((halfway, 1), (priming, 2)):
            resumed = {**headers, "last-event-id": str(resume_from)}
            async with http.stream("GET", "/mcp", headers=resumed) as stream:
                replayed = await _events(stream, expected)
            assert j(replayed[-1][1])["result"]["content"][0]["text"] == "done"
        await wire.close()


async def test_servers_without_numbered_events_drop_what_nobody_hears(tmp_path: Path) -> None:
    unnumbered = McpTransportModel(response_mode="json", get_stream="sse")
    app, _ = mcp_app(tmp_path, mcp_model(transport=unnumbered))
    fake = runtime_of_app(app).mcp
    assert fake is not None
    fake.keep_for_later("session", {"jsonrpc": "2.0", "method": "notifications/x"})
    assert fake._sent == {}
