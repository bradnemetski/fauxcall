"""A DeepWiki-like MCP model, recordings and app builders shared by the MCP engine tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2
import uvicorn

from fauxcall.clock import ControlledClock, ManualClock, datetime_to_ns
from fauxcall.config import FakeSettings
from fauxcall.model import (
    ApiModel,
    AuthModel,
    Body,
    CapturedRequest,
    CapturedResponse,
    ErrorTemplate,
    Exchange,
    McpModel,
    McpToolModel,
    McpTransportModel,
    RealTarget,
    SourceInfo,
)
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime import FakeRuntime, create_app, register_tool
from fauxcall.storage import write_model
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

RECORDED = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ASK: JsonObject = {
    "name": "ask_question",
    "description": "Ask a question about a GitHub repository.",
    "inputSchema": {
        "type": "object",
        "properties": {"repoName": {"type": "string"}, "question": {"type": "string"}},
        "required": ["repoName", "question"],
    },
    "x-unknown-vendor-field": {"kept": True},
}
STRUCTURE: JsonObject = {
    "name": "read_wiki_structure",
    "description": "List documentation topics.",
    "inputSchema": {
        "type": "object",
        "properties": {"repoName": {"type": "string"}},
        "required": ["repoName"],
    },
    "annotations": {"readOnlyHint": True},
}
CLOCK_TOOL: JsonObject = {
    "name": "now",
    "description": "Current time.",
    "inputSchema": {"type": "object", "properties": {}},
}


def mcp_model(
    *,
    transport: McpTransportModel | None = None,
    auth: AuthModel | None = None,
    tools: list[McpToolModel] | None = None,
    **mcp: Any,
) -> ApiModel:
    """The DeepWiki model: three tools (matched, replayed, computed) and recorded error shapes."""
    return ApiModel(
        name="deepwiki",
        title="DeepWiki",
        kind="mcp",
        source=SourceInfo(kind="mcp", url="https://mcp.deepwiki.test/mcp", fetched_at=RECORDED),
        real=RealTarget(base_url="https://mcp.deepwiki.test"),
        auth=auth,
        errors=[
            ErrorTemplate(
                name="unauthorized",
                status=401,
                content_type="application/json",
                body_json={"error": "invalid_token"},
            ),
            ErrorTemplate(
                name="forbidden",
                status=403,
                content_type="application/json",
                body_json={"error": "forbidden"},
            ),
        ],
        mcp=McpModel(
            transport=transport
            or McpTransportModel(
                response_mode="sse", protocol_versions={"2025-03-26": "2025-03-26"}
            ),
            initialize_result={
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "DeepWiki", "version": "2.14.3"},
                "instructions": "Use the tools to learn about repositories.",
            },
            tools=tools
            if tools is not None
            else [
                McpToolModel(definition=ASK, behavior="match"),
                McpToolModel(definition=STRUCTURE, behavior="replay"),
                McpToolModel(definition=CLOCK_TOOL, behavior="computed"),
            ],
            errors={
                "unknown_tool": {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "error": {"code": -32602, "message": "Unknown tool"},
                },
            },
            **mcp,
        ),
    )


def rpc_exchange(
    exchange_id: str, endpoint: str, method: str, params: JsonValue, reply: JsonValue
) -> Exchange:
    """A recorded JSON-RPC call (request ``id`` 7) and its reply."""
    sent: JsonObject = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        sent["params"] = params
    return Exchange(
        id=exchange_id,
        endpoint=endpoint,
        started_at=RECORDED,
        started_ns=datetime_to_ns(RECORDED),
        request=CapturedRequest(
            method=HttpMethod.POST,
            url="https://mcp.deepwiki.test/mcp",
            path="/mcp",
            body=Body(kind="json", json_value=sent),
        ),
        response=CapturedResponse(status=200, body=Body(kind="json", json_value=reply)),
    )


def tool_exchange(exchange_id: str, tool: str, arguments: JsonObject, text: str) -> Exchange:
    """A recorded ``tools/call`` answered with one text block."""
    reply: JsonValue = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }
    params: JsonValue = {"name": tool, "arguments": arguments}
    return rpc_exchange(exchange_id, f"MCP tools/call:{tool}", "tools/call", params, reply)


def mcp_app(
    tmp_path: Path, model: ApiModel, *extra: Exchange, **settings: Any
) -> tuple[FastAPI, FakeRuntime]:
    """Write the model and recordings, then build the app with a manual clock at ``RECORDED``."""
    data = tmp_path / "data"
    write_model(data / "model.json", model)
    store = RecordingStore(data / "recordings")
    recorded = [
        tool_exchange(
            "",
            "ask_question",
            {"repoName": "facebook/react", "question": "How does the reconciler work?"},
            "Fiber walks the tree.",
        ),
        tool_exchange(
            "",
            "ask_question",
            {"repoName": "facebook/react", "question": "What are hooks?"},
            "Hooks let components use state.",
        ),
        tool_exchange(
            "", "read_wiki_structure", {"repoName": "facebook/react"}, "1. Overview\n2. Fiber"
        ),
        *extra,
    ]
    for exchange in recorded:
        store.save(exchange.model_copy(update={"id": store.next_id()}))
    config = FakeSettings(data_dir=data, log_level="WARNING", **settings)
    runtime = FakeRuntime(
        config, model, clock=ControlledClock(ManualClock(datetime_to_ns(RECORDED)))
    )
    app = create_app(config, runtime=runtime, configure_logs=False)

    async def now_tool(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        del arguments
        return {"content": [{"type": "text", "text": str(ctx.now_ns)}]}

    if any(tool.name == "now" for tool in model.mcp.tools if model.mcp):  # type: ignore[union-attr]
        register_tool(app, "now", now_tool)
    return app, runtime


def asgi_client(app: FastAPI, **headers: str) -> httpx2.AsyncClient:
    """An in-process client for ``app``."""
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://fake.test", headers=headers
    )


@asynccontextmanager
async def live_server(app: FastAPI) -> AsyncIterator[str]:
    """Serve ``app`` with uvicorn on an ephemeral port: real sockets, real streaming."""
    # No log config or level: uvicorn must not reconfigure process-wide logging under other tests.
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        log_level=None,
        access_log=False,
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(1000):
        if server.started or task.done():
            break
        await asyncio.sleep(0.005)
    if not server.started:  # pragma: no cover - only when the port can't be bound
        await task
        raise RuntimeError("uvicorn did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
