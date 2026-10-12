"""What custom MCP tool handlers can answer besides a result, and what the fake repeats as recorded:
JSON-RPC errors, whole HTTP replies, per-session state, the caller's name in "unknown" errors, and
the extra members of listing results."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.responses import Response

from _engine import j
from _mcp import STRUCTURE, asgi_client, mcp_app, mcp_model
from fauxcall.build.scaffold import agent_headers, mcp_server_config
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.model import AuthModel, AuthScheme, McpToolModel, McpTransportModel
from fauxcall.model.mcp import PROBE_PROMPT, PROBE_RESOURCE, PROBE_TOOL
from fauxcall.model.realism import LatencyProfile
from fauxcall.runtime import EarlyResponse, RpcError, register_tool
from fauxcall.testing.asgi import streaming_client

if TYPE_CHECKING:
    from pathlib import Path

    import httpx2

    from fauxcall.runtime import RequestContext
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
