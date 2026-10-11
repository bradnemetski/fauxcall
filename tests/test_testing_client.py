from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import httpx2
import pytest

from fauxcall.config import FakeSettings
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.model.api import ApiModel, RealTarget, SourceInfo
from fauxcall.model.auth import AuthModel, AuthScheme
from fauxcall.model.mcp import McpModel, McpToolModel
from fauxcall.runtime import FakeRuntime, create_app, register_tool
from fauxcall.testing.client import (
    MCP_METHOD,
    PLACEHOLDER_KEY,
    PLACEHOLDER_PASSWORD,
    PLACEHOLDER_TOKEN,
    PLACEHOLDER_USER,
    ApiClient,
    CompareOptions,
    Credentials,
    ExchangeLog,
    McpClient,
    _observe,
    mcp_call_path,
    placeholder_credentials,
    with_query,
)
from fauxcall.testing.compare import Observation

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from fauxcall.privacy.pii import PiiCategory
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

ADD: JsonObject = {
    "name": "add",
    "description": "Add two numbers.",
    "inputSchema": {
        "type": "object",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
    },
    "annotations": {"readOnlyHint": True},
}
RESET: JsonObject = {
    "name": "reset",
    "description": "Forget everything.",
    "inputSchema": {"type": "object", "properties": {}},
}


def mcp_fake(data_dir: Path) -> FastAPI:
    """An in-process FauxCall MCP fake with two computed tools."""
    settings = FakeSettings(name="tools", data_dir=data_dir, fast=True, log_level="WARNING")
    model = ApiModel(
        name="tools",
        title="Tools",
        kind="mcp",
        source=SourceInfo(
            kind="mcp",
            url="https://mcp.example.com/mcp",
            fetched_at=datetime(2026, 10, 10, tzinfo=UTC),
        ),
        real=RealTarget(base_url="https://mcp.example.com"),
        mcp=McpModel(
            initialize_result={
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tools", "version": "1.0.0"},
            },
            tools=[
                McpToolModel(definition=ADD, behavior="computed"),
                McpToolModel(definition=RESET, behavior="computed"),
            ],
        ),
    )
    app = create_app(settings, runtime=FakeRuntime(settings, model), configure_logs=False)

    async def add(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        total = cast("float", arguments["a"]) + cast("float", arguments["b"])
        return {"content": [{"type": "text", "text": str(total)}], "isError": False}

    async def reset(_ctx: RequestContext, _arguments: JsonObject) -> JsonValue:
        return {"content": [{"type": "text", "text": "done"}]}

    register_tool(app, "add", add)
    register_tool(app, "reset", reset)
    return app


def mcp_client(
    data_dir: Path, *, target: str = "fake", allow_writes: bool = False
) -> tuple[McpClient, httpx2.AsyncClient]:
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=mcp_fake(data_dir)), base_url="http://fake.test"
    )
    client = McpClient(
        "real" if target == "real" else "fake",
        McpWireClient("http://fake.test/mcp", http),
        allow_writes=allow_writes,
        mutating=lambda name: name != "add",
    )
    return client, http


def rest_handler(request: httpx2.Request) -> httpx2.Response:
    match request.url.path:
        case "/json":
            return httpx2.Response(200, json={"id": 1, "price": 1.50})
        case "/badjson":
            return httpx2.Response(
                200, content=b"{oops", headers={"content-type": "application/json"}
            )
        case "/text":
            return httpx2.Response(200, text="hello", headers={"content-type": "text/plain"})
        case "/sse":
            return httpx2.Response(
                200, content=b"data: 1\n\n", headers={"content-type": "text/event-stream"}
            )
        case "/bin":
            return httpx2.Response(
                200, content=b"\x00\x01\x02", headers={"content-type": "application/octet-stream"}
            )
        case "/down":
            raise httpx2.ConnectError("connection refused", request=request)
        case _:
            return httpx2.Response(204)


def api_client(target: str = "fake", *, allow_writes: bool = False, **extra: Any) -> ApiClient:
    http = httpx2.AsyncClient(
        transport=httpx2.MockTransport(rest_handler), base_url="https://api.example.com"
    )
    return ApiClient(
        "real" if target == "real" else "fake", http, allow_writes=allow_writes, **extra
    )


# -- observations ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body", "text"),
    [
        ("/json", {"id": 1, "price": 1.5}, None),
        ("/badjson", None, "{oops"),
        ("/text", None, "hello"),
        ("/sse", None, "data: 1\n\n"),
        ("/bin", None, "<3 bytes>"),
        ("/empty", None, None),
    ],
)
async def test_observations_keep_json_text_or_size(
    path: str, body: object, text: str | None
) -> None:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(rest_handler)) as http:
        response = await http.get(f"https://api.example.com{path}")
    observation = _observe(response, 42)
    assert observation.body == body
    assert observation.text == text
    assert observation.at_ns == 42
    assert observation.status == response.status_code
    assert all(key == key.lower() for key in observation.headers)


# -- REST ------------------------------------------------------------------------------------------


async def test_calls_are_logged_with_labels_and_options() -> None:
    client = api_client()
    options = CompareOptions(values=False)
    before = time.time_ns()
    async with client.http:
        assert (await client.get("/json")).status_code == 200
        await client.post("/items", json={"a": 1}, label="create item", compare=options)
        await client.put("/items/1")
        await client.patch("/items/1")
        await client.delete("/items/1")
        await client.request("head", "/items/1")
    calls = client.log.calls
    assert [(c.method, c.path, c.label) for c in calls] == [
        ("GET", "/json", "GET /json"),
        ("POST", "/items", "create item"),
        ("PUT", "/items/1", "PUT /items/1"),
        ("PATCH", "/items/1", "PATCH /items/1"),
        ("DELETE", "/items/1", "DELETE /items/1"),
        ("HEAD", "/items/1", "HEAD /items/1"),
    ]
    assert calls[0].observation.body == {"id": 1, "price": 1.5}
    assert before <= calls[0].observation.at_ns <= time.time_ns()
    assert calls[0].options == CompareOptions()
    assert calls[1].options is options
    assert not client.is_real


async def test_transport_errors_are_logged_then_raised() -> None:
    client = api_client()
    async with client.http:
        with pytest.raises(httpx2.ConnectError):
            await client.get("/down")
    (call,) = client.log.calls
    assert call.observation.error == "ConnectError"
    assert call.observation.status is None
    assert call.label == "GET /down"


async def test_real_writes_need_permission() -> None:
    client = api_client("real")
    assert client.is_real
    async with client.http:
        assert (await client.get("/json")).status_code == 200
        with pytest.raises(pytest.skip.Exception, match="allow-real-writes"):
            await client.post("/items")
    assert len(client.log.calls) == 1
    allowed = api_client("real", allow_writes=True)
    async with allowed.http:
        assert (await allowed.delete("/items/1")).status_code == 204


def test_personal_values_as_each_side_knows_them() -> None:
    def translate(value: JsonValue, category: PiiCategory) -> JsonValue:
        return f"{category}:{value}"

    assert api_client(translate=translate).pii("jane@x.com", "email") == "email:jane@x.com"
    assert api_client("real", translate=translate).pii("jane@x.com", "email") == "jane@x.com"
    assert api_client().pii("jane@x.com", "email") == "jane@x.com"


# -- MCP -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "params", "path"),
    [
        ("tools/call", {"name": "ask", "arguments": {}}, "tools/call:ask"),
        ("prompts/get", {"name": "summary"}, "prompts/get:summary"),
        ("resources/read", {"uri": "file:///a.txt"}, "resources/read:file:///a.txt"),
        ("tools/list", {"cursor": "abc"}, "tools/list"),
        ("tools/call", {"arguments": {}}, "tools/call"),
        ("tools/call", {"name": ""}, "tools/call"),
        ("tools/call", None, "tools/call"),
        ("resources/read", {"uri": 7}, "resources/read"),
    ],
)
def test_mcp_call_paths(method: str, params: JsonValue, path: str) -> None:
    assert mcp_call_path(method, params) == path


async def test_mcp_client_logs_results_and_errors(tmp_path: Path) -> None:
    client, http = mcp_client(tmp_path)
    async with http:
        listing: Any = await client.list_tools()
        assert [tool["name"] for tool in listing["tools"]] == ["add", "reset"]
        result: Any = await client.call_tool("add", {"a": 1, "b": 2})
        assert result["content"][0]["text"] == "3"
        error: Any = await client.call_tool("missing", label="unknown tool")
        assert error["code"] == -32602
        await client.request("resources/read", {"uri": "file:///nothing"})
        await client.close()
        await client.close()  # idempotent
    calls = client.log.calls
    assert [(c.label, c.method, c.path, c.observation.status) for c in calls] == [
        ("tools/list", MCP_METHOD, "tools/list", 200),
        ("tools/call:add", MCP_METHOD, "tools/call:add", 200),
        ("unknown tool", MCP_METHOD, "tools/call:missing", 500),
        ("resources/read:file:///nothing", MCP_METHOD, "resources/read:file:///nothing", 500),
    ]
    assert calls[1].observation.headers == {"content-type": "application/json"}
    assert calls[1].observation.body == result


async def test_mcp_tools_that_may_write_need_permission_on_the_real_side(tmp_path: Path) -> None:
    client, http = mcp_client(tmp_path, target="real")
    async with http:
        assert await client.call_tool("add", {"a": 2, "b": 2}) is not None  # read-only tool
        with pytest.raises(pytest.skip.Exception, match="may modify state"):
            await client.call_tool("reset")
        await client.close()
    allowed, http = mcp_client(tmp_path, target="real", allow_writes=True)
    async with http:
        done: Any = await allowed.call_tool("reset")
        assert done["content"][0]["text"] == "done"
        await allowed.close()


async def test_closing_an_unopened_mcp_client_does_nothing(tmp_path: Path) -> None:
    client, http = mcp_client(tmp_path)
    async with http:
        await client.close()
    assert client.log.calls == []


class _BrokenWire:
    """Opens a session, then loses the connection."""

    def __init__(self) -> None:
        self.closed = 0

    async def initialize(self) -> None:
        return None

    async def call(self, method: str, params: JsonValue = None) -> None:
        del method, params
        raise httpx2.ReadTimeout("too slow")

    async def close(self) -> None:
        self.closed += 1


async def test_mcp_transport_errors_are_logged_then_raised() -> None:
    wire = _BrokenWire()
    client = McpClient(
        "fake", cast("McpWireClient", wire), allow_writes=False, mutating=lambda _name: False
    )
    with pytest.raises(httpx2.ReadTimeout):
        await client.call_tool("add", {"a": 1})
    (call,) = client.log.calls
    assert call.observation == Observation(
        None, {}, None, None, call.observation.at_ns, error="ReadTimeout"
    )
    assert call.path == "tools/call:add"
    await client.close()
    assert wire.closed == 1


# -- credentials -----------------------------------------------------------------------------------


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_no_auth_no_placeholders() -> None:
    assert placeholder_credentials(None) == Credentials()
    assert placeholder_credentials(AuthModel(schemes=[AuthScheme(kind="none")])) == Credentials()


def test_placeholders_for_every_scheme() -> None:
    auth = AuthModel(
        schemes=[
            AuthScheme(kind="api_key", location="header", name="X-Api-Key"),
            AuthScheme(kind="api_key", location="query", name="token", prefix="k-"),
            AuthScheme(kind="api_key", location="cookie", name="sid"),
            AuthScheme(kind="oauth2", prefix="Bearer "),
            AuthScheme(kind="basic"),
        ]
    )
    assert placeholder_credentials(auth) == Credentials(
        headers={"X-Api-Key": PLACEHOLDER_KEY, "Authorization": f"Bearer {PLACEHOLDER_TOKEN}"},
        query={"token": f"k-{PLACEHOLDER_KEY}"},
        cookies={"sid": PLACEHOLDER_KEY},
    )


def test_the_first_authorization_scheme_wins() -> None:
    basic_first = AuthModel(schemes=[AuthScheme(kind="basic"), AuthScheme(kind="bearer")])
    assert placeholder_credentials(basic_first).headers == {
        "Authorization": basic(PLACEHOLDER_USER, PLACEHOLDER_PASSWORD)
    }
    token_key = AuthModel(
        schemes=[
            AuthScheme(kind="api_key", location="header", name="Authorization", prefix="Token "),
            AuthScheme(kind="bearer"),
            AuthScheme(kind="api_key", location="header", name="authorization"),
        ]
    )
    assert placeholder_credentials(token_key).headers == {
        "Authorization": f"Token {PLACEHOLDER_KEY}"
    }
    bearer = AuthModel(schemes=[AuthScheme(kind="bearer", prefix="JWT ")])
    assert placeholder_credentials(bearer).headers == {"Authorization": f"JWT {PLACEHOLDER_TOKEN}"}


def test_placeholders_follow_the_credential_format() -> None:
    hex32 = AuthScheme(kind="api_key", location="header", name="X-Key", format_regex="[0-9a-f]{32}")
    uuid = AuthScheme(kind="bearer", format_regex=r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
    impossible = AuthScheme(kind="basic", format_regex="z{3}")
    assert placeholder_credentials(AuthModel(schemes=[hex32, uuid])).headers == {
        "X-Key": "0123456789abcdef0123456789abcdef",
        "Authorization": "Bearer 00000000-0000-4000-8000-000000000000",
    }
    assert placeholder_credentials(AuthModel(schemes=[impossible])).headers == {
        "Authorization": basic(PLACEHOLDER_USER, PLACEHOLDER_PASSWORD)
    }
    broken = AuthScheme(kind="api_key", location="query", name="k", format_regex="(")
    assert placeholder_credentials(AuthModel(schemes=[broken])).query == {"k": PLACEHOLDER_KEY}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/pets", "/pets?token=t&extra=e"),
        ("/pets?status=sold", "/pets?status=sold&token=t&extra=e"),
        ("/pets?token=mine", "/pets?token=mine&extra=e"),
        ("/pets?token=&extra=x", "/pets?token=&extra=x"),
    ],
)
def test_query_credentials_never_replace_the_paths_own(path: str, expected: str) -> None:
    assert with_query(path, {"token": "t", "extra": "e"}) == expected


def _seen(seen: list[str]) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, json={})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), base_url="https://x.test")


async def test_query_credentials_are_sent_but_never_logged() -> None:
    seen: list[str] = []
    shared = ExchangeLog()
    client = ApiClient(
        "real", _seen(seen), allow_writes=False, query={"token": "s3cret"}, log=shared
    )
    async with client.http:
        await client.get("/pets?status=sold")
        await client.get("/pets", params={"status": "sold"})
        await client.get("/pets", params={"token": "other"})
        await client.get("/pets", params=[("tag", "a"), ("tag", "b")])
    assert seen == [
        "https://x.test/pets?status=sold&token=s3cret",
        "https://x.test/pets?status=sold&token=s3cret",
        "https://x.test/pets?token=other",
        "https://x.test/pets?tag=a&tag=b&token=s3cret",
    ]
    assert client.log is shared
    assert [call.path for call in shared.calls] == ["/pets?status=sold", "/pets", "/pets", "/pets"]
    assert "s3cret" not in repr(shared)
    plain = ApiClient("fake", _seen(seen), allow_writes=False)
    async with plain.http:
        await plain.get("/pets?x=1")
    assert seen[-1] == "https://x.test/pets?x=1"


async def test_mcp_clients_can_share_a_log(tmp_path: Path) -> None:
    shared = ExchangeLog()
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=mcp_fake(tmp_path)), base_url="http://fake.test"
    )
    client = McpClient(
        "fake",
        McpWireClient("http://fake.test/mcp", http),
        allow_writes=False,
        mutating=lambda _name: False,
        log=shared,
    )
    async with http:
        await client.list_tools()
        await client.close()
    assert client.log is shared
    assert [call.path for call in shared.calls] == ["tools/list"]
