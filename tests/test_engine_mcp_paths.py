"""Fake MCP server branches: listings and pagination, resources and prompts, argument validation
modes, failing and computed tools, latency, faults, protocol errors, the journal and GET streams."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from _engine import j
from _mcp import (
    ASK,
    STRUCTURE,
    asgi_client,
    live_server,
    mcp_app,
    mcp_model,
    rpc_exchange,
)
from fauxcall.config import FaultRule
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.model import McpToolModel, McpTransportModel
from fauxcall.model.realism import LatencyProfile
from fauxcall.model.semantic import MatchSpec, SemanticModel
from fauxcall.runtime.mcp_server import flatten_args, method_endpoint, tool_endpoint

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from fauxcall.types import JsonValue

JSON_MODE = McpTransportModel(
    response_mode="json",
    issues_session=False,
    requires_session=False,
    protocol_versions={"2024-11-05": "2024-11-05"},
)
README = "repo://facebook/react/readme"


async def _rpc(http: httpx2.AsyncClient, method: str, params: JsonValue = None) -> Any:
    """POST one request (JSON response mode) and return the decoded JSON-RPC response."""
    response = await http.post("/mcp", json=protocol.request(1, method, params))
    assert response.status_code == 200
    return j(response.json())


def _json_app(tmp_path: Path, **kwargs: Any) -> tuple[FastAPI, Any]:
    extra = kwargs.pop("extra", ())
    settings = kwargs.pop("settings", {})
    return mcp_app(tmp_path, mcp_model(transport=JSON_MODE, **kwargs), *extra, **settings)


def test_endpoint_ids_and_argument_flattening() -> None:
    assert tool_endpoint("ask") == "MCP tools/call:ask"
    assert method_endpoint("resources/read", README) == f"MCP resources/read:{README}"
    assert method_endpoint("ping") == "MCP ping"
    nested: JsonValue = {"a": {"b": 1}, "c": [2, 3], "d e": [{"f": 1}, {"f": 2}]}
    assert flatten_args(nested) == {"args.a.b": 1, "args.c[*]": [2, 3], 'args["d e"][*].f': [1, 2]}
    assert flatten_args(None) == {}
    assert flatten_args([1]) == {}


async def test_tool_pagination_and_listings(tmp_path: Path) -> None:
    app, _ = _json_app(
        tmp_path,
        tools_page_size=2,
        resources=[{"uri": README, "name": "readme"}],
        resource_templates=[{"uriTemplate": "repo://{owner}/{name}/readme", "name": "readme"}],
        prompts=[{"name": "summarize", "description": "Summarize a repository"}],
    )
    async with asgi_client(app) as http:
        first = (await _rpc(http, "tools/list"))["result"]
        assert [t["name"] for t in first["tools"]] == ["ask_question", "read_wiki_structure"]
        second = (await _rpc(http, "tools/list", {"cursor": first["nextCursor"]}))["result"]
        assert [t["name"] for t in second["tools"]] == ["now"]
        assert "nextCursor" not in second
        for cursor in ("!!not base64!!", base64.urlsafe_b64encode(b"99").decode()):
            bad = await _rpc(http, "tools/list", {"cursor": cursor})
            assert bad["error"] == {"code": protocol.INVALID_PARAMS, "message": "Invalid cursor"}
        resources = (await _rpc(http, "resources/list"))["result"]
        assert resources == {"resources": [{"uri": README, "name": "readme"}]}
        templates = (await _rpc(http, "resources/templates/list"))["result"]
        assert templates["resourceTemplates"][0]["name"] == "readme"
        prompts = (await _rpc(http, "prompts/list"))["result"]
        assert prompts["prompts"][0]["name"] == "summarize"
        assert (await _rpc(http, "logging/setLevel", {"level": "info"}))["result"] == {}


async def test_resources_and_prompts_replay_recordings(tmp_path: Path) -> None:
    recorded_read = rpc_exchange(
        "",
        method_endpoint("resources/read", README),
        "resources/read",
        {"uri": README},
        {"jsonrpc": "2.0", "id": 7, "result": {"contents": [{"uri": README, "text": "React"}]}},
    )
    app, _ = _json_app(tmp_path, extra=[recorded_read])
    async with asgi_client(app) as http:
        read = await _rpc(http, "resources/read", {"uri": README})
        assert read["id"] == 1  # re-addressed to this request
        assert read["result"]["contents"][0]["text"] == "React"
        unknown = await _rpc(http, "resources/read", {"uri": "repo://nope"})
        assert unknown["error"]["code"] == protocol.INVALID_PARAMS
        prompt = await _rpc(http, "prompts/get", {"name": "summarize"})
        assert prompt["error"] == {"code": protocol.INVALID_PARAMS, "message": "Not found"}


@pytest.mark.parametrize(
    ("requested", "initialize_result", "expected"),
    [
        ("2024-11-05", None, "2024-11-05"),  # mapped explicitly
        ("1999-01-01", None, "2025-06-18"),  # unknown: the transport default
        ("1999-01-01", {"protocolVersion": "2025-03-26"}, "2025-03-26"),  # what the server said
    ],
)
async def test_protocol_version_negotiation(
    tmp_path: Path, requested: str, initialize_result: JsonValue, expected: str
) -> None:
    model = mcp_model(transport=JSON_MODE)
    assert model.mcp is not None
    model.mcp.initialize_result = initialize_result
    app, _ = mcp_app(tmp_path, model)
    async with asgi_client(app) as http:
        reply = await _rpc(http, "initialize", {"protocolVersion": requested})
    assert reply["result"]["protocolVersion"] == expected


async def test_argument_validation_modes(tmp_path: Path) -> None:
    template: JsonValue = {
        "jsonrpc": "2.0",
        "id": 0,
        "error": {"code": -32602, "message": "Invalid input: repoName must be a string"},
    }
    tools = [
        McpToolModel(definition=ASK, behavior="match", invalid_args="jsonrpc_error"),
        McpToolModel(definition=STRUCTURE, behavior="replay", invalid_args="accept"),
        McpToolModel(definition={**STRUCTURE, "name": "templated"}, invalid_template=template),
    ]
    app, _ = _json_app(tmp_path, tools=tools)
    async with asgi_client(app) as http:
        rpc_error = await _rpc(http, "tools/call", {"name": "ask_question", "arguments": {}})
        assert rpc_error["error"]["code"] == protocol.INVALID_PARAMS
        assert rpc_error["error"]["message"].startswith("Invalid arguments: ")
        accepted = await _rpc(
            http, "tools/call", {"name": "read_wiki_structure", "arguments": {"repoName": 5}}
        )
        assert accepted["result"]["content"][0]["text"] == "1. Overview\n2. Fiber"
        templated = await _rpc(http, "tools/call", {"name": "templated", "arguments": {}})
        assert templated == {**j(template), "id": 1}


async def test_failing_computed_and_unrecorded_tools(tmp_path: Path) -> None:
    def tool(name: str, **settings: Any) -> McpToolModel:
        definition: JsonValue = {"name": name, "inputSchema": {"type": "object"}}
        return McpToolModel(definition=definition, **settings)

    failure: JsonValue = {"jsonrpc": "2.0", "id": 0, "result": {"isError": True, "content": []}}
    tools = [
        tool("broken", behavior="error"),
        tool("broken_recorded", behavior="error", invalid_template=failure),
        tool("calc", behavior="computed"),
        tool("fresh", behavior="replay"),
    ]
    app, _ = _json_app(tmp_path, tools=tools)
    async with asgi_client(app) as http:
        broken = (await _rpc(http, "tools/call", {"name": "broken"}))["result"]
        assert broken == {
            "content": [{"type": "text", "text": "Error executing tool broken"}],
            "isError": True,
        }
        recorded = await _rpc(http, "tools/call", {"name": "broken_recorded"})
        assert recorded == {**j(failure), "id": 1}
        calc = await _rpc(http, "tools/call", {"name": "calc", "arguments": {}})
        assert calc["error"]["message"] == "fauxcall: no handler for calc"
        fresh = await _rpc(http, "tools/call", {"name": "fresh", "arguments": {}})
        assert fresh["error"] == {"code": protocol.INTERNAL_ERROR, "message": "No matching result"}


async def test_latency_faults_and_journal(tmp_path: Path) -> None:
    tools = [
        McpToolModel(
            definition=STRUCTURE, behavior="replay", latency=LatencyProfile.from_samples([40.0])
        )
    ]
    faults = [
        FaultRule(endpoint="MCP tools/list", kind="error"),
        FaultRule(endpoint="MCP ping", kind="delay", delay_ms=25),
    ]
    app, runtime = _json_app(tmp_path, tools=tools, settings={"latency": True, "faults": faults})
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    runtime._sleep = record_sleep
    call: JsonValue = {"name": "read_wiki_structure", "arguments": {"repoName": "facebook/react"}}
    async with asgi_client(app) as http:
        assert "result" in await _rpc(http, "tools/call", call)
        injected = await _rpc(http, "tools/list")
        assert injected["error"]["message"] == "fauxcall injected error"
        assert (await _rpc(http, "ping"))["result"] == {}
    assert sleeps == pytest.approx([0.04, 0.025])
    entries = runtime.journal.query(path_prefix="/mcp")
    assert [e.mcp_method for e in entries] == ["tools/call", "tools/list", "ping"]
    assert [e.fault for e in entries] == [None, "error", "delay"]


async def test_journal_can_be_disabled(tmp_path: Path) -> None:
    app, runtime = _json_app(tmp_path, settings={"journal_limit": 0})
    async with asgi_client(app) as http:
        assert (await _rpc(http, "ping"))["result"] == {}
    assert runtime.journal.query() == []


async def test_transport_edges(tmp_path: Path) -> None:
    app, _ = mcp_app(tmp_path, mcp_model())  # SSE answers, sessions required
    async with asgi_client(app) as http:
        broken = await http.post("/mcp", content=b"{", headers={"content-type": "application/json"})
        assert broken.status_code == 400
        assert broken.json()["error"]["code"] == protocol.PARSE_ERROR
        assert (await http.delete("/mcp")).status_code == 200  # no session: nothing to end
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        notified = await wire.notify("notifications/initialized")
        assert notified.status == 202
        batch = await http.post(
            "/mcp",
            json=[protocol.request(1, "ping"), protocol.request(2, "ping")],
            headers={protocol.SESSION_HEADER: wire.session_id or ""},
        )
        events = protocol.parse_sse(batch.text)
        assert [j(protocol.messages_from_body(e[1], "application/json"))[0]["id"] for e in events]
        assert len(events) == 2


async def test_get_stream_when_the_server_offers_one(tmp_path: Path) -> None:
    transport = McpTransportModel(get_stream="sse", sse_event_ids=True)
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport))
    async with live_server(app) as base, httpx2.AsyncClient() as http:
        wire = McpWireClient(f"{base}/mcp", http)
        init = await wire.initialize()
        assert init.sse_events
        assert init.sse_events[0][3]  # event ids are sent when the real server sends them
        status, content_type = await wire.get_stream_status()
        assert status == 200
        assert content_type is not None
        assert content_type.startswith("text/event-stream")
        await wire.close()


async def test_unmatched_calls_can_answer_with_empty_content(tmp_path: Path) -> None:
    definition: JsonValue = {"name": "search", "inputSchema": {"type": "object"}}
    semantic = SemanticModel(
        match=MatchSpec(inputs=["args.q"], exact=["args.q"], fallback="none", no_match="empty")
    )
    tools = [McpToolModel(definition=definition, behavior="match", semantic=semantic)]
    app, _ = _json_app(tmp_path, tools=tools)
    async with asgi_client(app) as http:
        reply = await _rpc(http, "tools/call", {"name": "search", "arguments": {"q": "nothing"}})
    assert reply["result"] == {"content": []}


@pytest.mark.parametrize(("status", "allow"), [(405, "POST, DELETE"), (404, None)])
async def test_refused_get_answers_with_the_recorded_status(
    tmp_path: Path, status: int, allow: str | None
) -> None:
    app, _ = mcp_app(tmp_path, mcp_model(transport=McpTransportModel(get_status=status)))
    async with asgi_client(app) as http:
        refused = await http.get("/mcp", headers={"accept": "text/event-stream"})
    assert refused.status_code == status
    assert refused.headers.get("allow") == allow
