from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from fauxcall import jsonfmt
from fauxcall.errors import HttpStatusError, RecordingError
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.model import (
    Approval,
    Approvals,
    McpToolModel,
    McpTransportModel,
    PlannedCall,
    RecordingPlan,
)
from fauxcall.model.mcp import PROBE_PROMPT, PROBE_RESOURCE, PROBE_TOOL
from fauxcall.recording import McpRecorder, RecordingStore, tool_is_mutating
from fauxcall.recording.mcp import (
    PROBE_METHOD,
    _listing_extras,
    _may_pass,
    _method_of,
    _params_for,
    _static_scheme,
    answer_of,
    needs_approval,
)
from fauxcall.recording.privacy_filter import PrivacyConfig, RecordingPrivacy
from fauxcall.storage import read_model, write_model
from fauxcall.testing.asgi import streaming_client
from test_mcpwire_transport import (
    DELETE_ITEM,
    ECHO,
    WHOAMI,
    fake_app,
    fake_model,
    sdk_client,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fauxcall.model import McpModel
    from fauxcall.types import JsonValue

TOKEN = "sk-mcp-0123456789"


def j(value: object) -> Any:
    return value


class FakeClock:
    def __init__(self) -> None:
        self.now = 50.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


def recorder(
    tmp_path: Path,
    http: httpx2.AsyncClient,
    url: str,
    clock: FakeClock | None = None,
    **options: Any,
) -> McpRecorder:
    clock = clock or FakeClock()
    return McpRecorder(
        RecordingStore(tmp_path / "recordings"),
        url,
        http,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **options,
    )


def endpoints(tmp_path: Path) -> list[str]:
    return [e.endpoint for _, e in RecordingStore(tmp_path / "recordings").iter_exchanges()]


def plan(*calls: dict[str, Any]) -> RecordingPlan:
    return RecordingPlan.model_validate(
        {"name": "mcp", "created_at": "2026-10-10T12:00:00Z", "calls": list(calls)}
    )


def tool_call(tool: str, arguments: JsonValue = None, **extra: Any) -> dict[str, Any]:
    body = {"arguments": arguments} if arguments is not None else None
    request: dict[str, Any] = {"method": "POST", "path": "/mcp"}
    if body is not None:
        request["json_body"] = body
    return {
        "id": extra.pop("id", tool),
        "endpoint": f"MCP tools/call:{tool}",
        "request": request,
        **extra,
    }


# -- helpers ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("definition", "mutating"),
    [
        (None, True),
        ({"name": "t"}, True),
        ({"name": "t", "annotations": "odd"}, True),
        ({"name": "t", "annotations": {"readOnlyHint": False}}, True),
        ({"name": "t", "annotations": {"readOnlyHint": "yes"}}, True),
        ({"name": "t", "annotations": {"readOnlyHint": True}}, False),
    ],
)
def test_tool_is_mutating(definition: JsonValue, mutating: bool) -> None:
    assert tool_is_mutating(definition) is mutating


@pytest.mark.parametrize(
    ("endpoint", "params", "expected"),
    [
        ("MCP tools/call:ask", None, {"name": "ask"}),
        ("MCP tools/call:ask", {"arguments": {"q": 1}}, {"name": "ask", "arguments": {"q": 1}}),
        ("MCP tools/call:ask", {"name": "other"}, {"name": "other"}),
        ("MCP resources/read:demo://x", None, {"uri": "demo://x"}),
        ("MCP prompts/get:summarize", {"arguments": {}}, {"name": "summarize", "arguments": {}}),
        ("MCP tools/call", {"name": "x"}, {"name": "x"}),
        ("MCP ping", None, None),
        ("MCP tools/call:ask", ["odd"], ["odd"]),
    ],
)
def test_params_for(endpoint: str, params: JsonValue, expected: JsonValue) -> None:
    assert _params_for(endpoint, params) == expected


def test_listing_extras_keep_the_real_results_other_members_in_order() -> None:
    result: JsonValue = {"resultType": "x", "tools": [1], "nextCursor": "c", "_meta": {"a": 1}}
    assert _listing_extras(result, "tools") == {"resultType": "x", "tools": None, "_meta": {"a": 1}}
    assert _listing_extras({"tools": [], "nextCursor": "c"}, "tools") is None  # nothing more
    assert _listing_extras({"other": 1}, "tools") is None
    assert _listing_extras(None, "tools") is None


def test_unknown_tool_names_are_probes_not_writes() -> None:
    def call(tool: str) -> PlannedCall:
        return PlannedCall.model_validate(tool_call(tool, {}))

    listed: dict[str, JsonValue] = {"echo": ECHO, "delete_item": DELETE_ITEM}
    assert not needs_approval(call("make_coffee"), listed)  # refused as unknown
    assert needs_approval(call("delete_item"), listed)
    assert not needs_approval(call("echo"), listed)
    assert needs_approval(call("make_coffee"), {})  # no listing to tell: may modify state


def testanswer_of_names_how_each_reply_answered() -> None:
    assert answer_of({"jsonrpc": "2.0", "id": 1, "result": {"content": []}}) == "ok"
    assert answer_of({"result": {"content": [], "isError": True}}) == "isError"
    assert answer_of({"error": {"code": -32602, "message": "bad"}}) == "error -32602"
    assert answer_of(None) == "no reply"


def test_only_passing_failures_are_tried_again() -> None:
    request = httpx2.Request("POST", "http://mock/mcp")
    assert _may_pass(httpx2.ConnectError("dropped", request=request))
    assert _may_pass(TimeoutError())
    assert _may_pass(HttpStatusError("busy", 503))
    assert _may_pass(HttpStatusError("not now", 429))
    assert not _may_pass(HttpStatusError("no such path", 404))
    assert not _may_pass(RecordingError("not MCP"))


def test_method_of() -> None:
    assert _method_of("MCP tools/call:ask_question") == "tools/call"
    assert _method_of("MCP resources/read:demo://a:b") == "resources/read"
    assert _method_of("MCP ping") == "ping"


# -- introspection ---------------------------------------------------------------------------------


def _check_round_trip(original: McpModel, again: McpModel) -> None:
    assert again.transport == original.transport
    assert again.initialize_result == original.initialize_result
    assert [t.definition for t in again.tools] == [t.definition for t in original.tools]
    assert again.tools_page_size == original.tools_page_size
    assert again.errors == original.errors
    assert (again.resources, again.prompts) == (original.resources, original.prompts)


@pytest.mark.parametrize("mode", ["json", "sse"])
async def test_introspect_the_fake(tmp_path: Path, mode: str) -> None:
    transport = McpTransportModel(
        response_mode=mode,  # type: ignore[arg-type]
        get_stream="sse" if mode == "sse" else "status",
        get_status=404,
        delete_status=204,
        sse_event_ids=mode == "sse",
        missing_session_status=401,
        protocol_versions={"2025-03-26": "2025-03-26", "2024-11-05": "2025-03-26"},
    )
    app = fake_app(tmp_path / "a", fake_model(transport=transport, page_size=2))
    clock = FakeClock()
    async with streaming_client(app) as http:
        found = await recorder(tmp_path, http, "http://fake.test/mcp", clock).introspect()
    model = found.model
    assert [t.name for t in model.tools] == ["echo", "delete_item", "whoami"]
    assert model.tools[0].definition == ECHO  # verbatim
    assert model.tools_page_size == 2
    assert model.prompts == [{"name": "summarize", "description": "Summarize a topic."}]
    assert model.resources == []  # not advertised, so not listed
    assert j(model.initialize_result)["serverInfo"] == {"name": "Demo", "version": "1.0.0"}
    learned = model.transport
    assert (learned.kind, learned.path, learned.response_mode) == ("streamable_http", "/mcp", mode)
    assert learned.issues_session
    assert learned.requires_session
    assert learned.missing_session_status == 401
    assert learned.get_stream == transport.get_stream
    if mode == "json":
        assert learned.get_status == 404  # whatever the server answered, not always 405
    assert learned.delete_status == 204
    assert learned.sse_event_ids is (mode == "sse")
    assert learned.default_protocol_version == "2025-06-18"
    assert learned.protocol_versions == {
        "2024-11-05": "2025-03-26",
        "2025-03-26": "2025-03-26",
        "2025-06-18": "2025-06-18",
        "2025-11-25": "2025-06-18",
    }
    if mode == "json":
        assert learned.content_type_json == "application/json"
    else:
        assert learned.content_type_sse == "text/event-stream; charset=utf-8"
    assert set(model.errors) == {
        "method_not_found",
        "unknown_tool",
        "missing_session",
        "prompts/get_not_found",  # prompts are advertised, resources aren't
    }
    assert j(model.errors["missing_session"]) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Bad Request: No valid session ID provided"},
    }
    assert j(model.errors["method_not_found"])["error"]["code"] == protocol.METHOD_NOT_FOUND
    assert j(model.errors["unknown_tool"])["error"]["message"] == f"Unknown tool: {PROBE_TOOL}"
    assert j(model.errors["prompts/get_not_found"])["error"]["message"] == "Not found"
    assert found.notes == ["3 tools, 0 resources, 1 prompts"]
    assert endpoints(tmp_path) == [
        "MCP initialize",
        "MCP tools/list",
        "MCP tools/list",
        "MCP prompts/list",
        "MCP tools/list:missing-session",
        f"MCP {PROBE_METHOD}",
        f"MCP tools/call:{PROBE_TOOL}",
        f"MCP prompts/get:{PROBE_PROMPT}",
        *["MCP tools/list:with-sampling"] * 2,  # two pages each
        *["MCP tools/list:with-elicitation"] * 2,
        *["MCP tools/list:with-roots"] * 2,
        "MCP initialize:version-2024-11-05",
        "MCP initialize:version-2025-03-26",
        "MCP initialize:version-2025-06-18",
        "MCP initialize:version-2025-11-25",
    ]
    assert len(found.exchanges) == 18
    # every request was paced: 2 rps -> 0.5 s apart (cleanup DELETEs included)
    assert clock.sleeps == [0.5] * 29  # each capability probe: initialize, 2 pages, DELETE
    # A fake built from what was learned behaves the same on the wire.
    rebuilt = fake_model(transport=model.transport, page_size=model.tools_page_size)
    rebuilt.mcp = model.model_copy(update={"tools": rebuilt.mcp.tools if rebuilt.mcp else []})
    async with streaming_client(fake_app(tmp_path / "b", rebuilt)) as http:
        again = await recorder(tmp_path / "b", http, "http://fake.test/mcp").introspect()
    _check_round_trip(model, again.model)


async def test_introspect_the_official_sdk_server(tmp_path: Path) -> None:
    async with sdk_client() as http:
        found = await recorder(tmp_path, http, "http://sdk.test/mcp", rps=0).introspect()
    model = found.model
    assert sorted(t.name for t in model.tools) == ["delete_item", "echo"]
    assert [r["uri"] for r in j(model.resources)] == ["demo://greeting"]
    assert model.resource_templates == []
    assert [p["name"] for p in j(model.prompts)] == ["summarize"]
    learned = model.transport
    assert learned.response_mode == "sse"
    assert learned.content_type_sse == "text/event-stream"
    assert learned.get_stream == "sse"
    assert (learned.missing_session_status, learned.requires_session) == (400, True)
    assert learned.delete_status == 200
    assert learned.protocol_versions == {v: v for v in protocol.KNOWN_VERSIONS}
    assert j(model.errors["missing_session"])["error"] == {
        "code": -32600,
        "message": "Bad Request: Missing session ID",
    }
    assert j(model.errors["unknown_tool"])["result"]["isError"] is True
    assert j(model.errors["method_not_found"])["error"]["code"] == protocol.METHOD_NOT_FOUND
    assert j(model.errors["resources/read_not_found"])["error"]["data"] == {"uri": PROBE_RESOURCE}
    assert j(model.errors["prompts/get_not_found"])["error"]["message"] == (
        f"Unknown prompt: {PROBE_PROMPT}"
    )
    # FauxCall's fake, built from the recording, answers like the SDK server.
    fake = fake_model(transport=learned)
    fake.mcp = model.model_copy(update={"tools": fake.mcp.tools if fake.mcp else []})
    async with streaming_client(fake_app(tmp_path / "fake", fake)) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize(protocol_version="2025-03-26")
        assert wire.negotiated_version == "2025-03-26"
        unknown = await wire.call("tools/call", {"name": "nope", "arguments": {}})
        assert j(unknown.result)["isError"] is True
        # The caller's names, where the real errors repeated the probes'
        missing = await wire.call("resources/read", {"uri": "demo://nope"})
        assert j(missing.error)["message"] == "Unknown resource: demo://nope"
        assert j(missing.error)["data"] == {"uri": "demo://nope"}
        missing = await wire.call("prompts/get", {"name": "frobnicate"})
        assert j(missing.error)["message"] == "Unknown prompt: frobnicate"
        no_session = await wire.post(protocol.request(3, "tools/list"), include_session=False)
        assert (no_session.status, no_session.response) == (400, model.errors["missing_session"])
        assert (await wire.get_stream_status())[0] == 200
        await wire.close()


async def test_introspect_keeps_the_listings_other_members(tmp_path: Path) -> None:
    extras: JsonValue = {"tools": None, "ttlMs": 60000, "_meta": {"region": "eu"}}
    model = fake_model(listing_extras={"tools/list": extras})
    async with streaming_client(fake_app(tmp_path / "a", model)) as http:
        found = await recorder(tmp_path, http, "http://fake.test/mcp", rps=0).introspect(
            probe_versions=False
        )
    assert found.model.listing_extras == {"tools/list": extras}


async def test_tools_only_some_clients_see(tmp_path: Path) -> None:
    sample: JsonValue = {"name": "ask_the_model", "inputSchema": {"type": "object"}}
    model = fake_model()
    assert model.mcp is not None
    model.mcp.tools.append(McpToolModel(definition=sample, requires=["sampling"]))
    either: JsonValue = {"name": "ask_either", "inputSchema": {"type": "object"}}
    model.mcp.tools.append(McpToolModel(definition=either, requires=["sampling", "roots"]))
    async with streaming_client(fake_app(tmp_path / "a", model)) as http:
        found = await recorder(tmp_path, http, "http://fake.test/mcp", rps=0).introspect(
            probe_versions=False
        )
        learned = found.model.tool("ask_the_model")
        assert learned is not None
        assert learned.requires == ["sampling"]
        both = found.model.tool("ask_either")
        assert both is not None
        assert both.requires == ["roots", "sampling"]  # listed to clients with either
        assert [t.name for t in found.model.tools if not t.requires] == [
            "echo",
            "delete_item",
            "whoami",
        ]
        plain = McpWireClient("http://fake.test/mcp", http)
        await plain.initialize()
        listed = j((await plain.call("tools/list")).result)["tools"]
        assert "ask_the_model" not in [t["name"] for t in listed]
        hidden = await plain.call("tools/call", {"name": "ask_the_model", "arguments": {}})
        assert hidden.error is not None or j(hidden.result)["isError"] is True
        capable = McpWireClient("http://fake.test/mcp", http)
        await capable.initialize(capabilities={"sampling": {}})
        listed = j((await capable.call("tools/list")).result)["tools"]
        assert "ask_the_model" in [t["name"] for t in listed]
        await plain.close()
        await capable.close()


async def test_a_capability_probe_that_fails_is_skipped(tmp_path: Path) -> None:
    server = Scripted()

    def answer(request: httpx2.Request) -> httpx2.Response:
        if b'"sampling"' in request.content:
            return httpx2.Response(500, text="boom")
        return server(request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as http:
        found = await recorder(tmp_path, http, "http://mock/mcp", rps=0).introspect(
            probe_versions=False
        )
    assert [t.name for t in found.model.tools] == ["echo"]
    assert "MCP tools/list:with-elicitation" in endpoints(tmp_path)  # the others still ran
    assert "MCP tools/list:with-sampling" not in endpoints(tmp_path)


async def test_introspect_falls_back_to_the_legacy_transport(tmp_path: Path) -> None:
    transport = McpTransportModel(kind="sse", path="/sse", messages_path="/rpc/messages/")
    app = fake_app(tmp_path, fake_model(transport=transport))
    async with streaming_client(app) as http:
        found = await recorder(tmp_path, http, "http://fake.test/sse", rps=0).introspect()
    learned = found.model.transport
    assert (learned.kind, learned.path, learned.messages_path) == ("sse", "/sse", "/rpc/messages/")
    assert learned.response_mode == "sse"
    assert learned.content_type_sse == "text/event-stream; charset=utf-8"
    assert learned.issues_session is False
    assert learned.protocol_versions == {}  # only probed over Streamable HTTP
    assert "missing_session" not in found.model.errors
    assert endpoints(tmp_path)[0] == "MCP initialize"
    assert len(found.model.tools) == 3


async def test_introspect_fails_cleanly(tmp_path: Path) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(404))
    ) as http:
        rec = recorder(tmp_path, http, "http://mock/mcp")
        with pytest.raises(RecordingError, match="does not speak MCP over Streamable HTTP or SSE"):
            await rec.introspect()
    assert rec.calls_made == 2


CHALLENGE = 'Bearer resource_metadata="https://mock/.well-known/oauth-protected-resource"'


def _refusing(status: int) -> httpx2.MockTransport:
    return httpx2.MockTransport(
        lambda _: httpx2.Response(status, headers={"WWW-Authenticate": CHALLENGE})
    )


@pytest.mark.parametrize(
    ("headers", "advice"),
    [
        ({}, "No credentials were sent: put them in the fake's .env as FAUXCALL_REAL__HEADERS"),
        ({"Authorization": "sk_test_abc"}, "The Authorization value has no scheme; most servers"),
        ({"Authorization": "Bearer sk_test_abc"}, "Check that the credentials in"),
    ],
)
async def test_introspect_says_when_the_credentials_are_refused(
    tmp_path: Path, headers: dict[str, str], advice: str
) -> None:
    async with httpx2.AsyncClient(transport=_refusing(401)) as http:
        rec = recorder(tmp_path, http, "http://mock/mcp", headers=headers)
        with pytest.raises(RecordingError) as caught:
            await rec.introspect()
    message = str(caught.value)
    assert message.startswith("http://mock/mcp refused the credentials: HTTP 401")
    assert f"(WWW-Authenticate: {CHALLENGE})" in message
    assert advice in message
    assert "sk_test_abc" not in message  # never the credential itself
    assert rec.calls_made == 1  # a refusal isn't retried, nor tried over SSE


async def test_introspect_names_what_each_transport_answered(tmp_path: Path) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404 if request.method == "POST" else 410)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as http:
        with pytest.raises(RecordingError) as caught:
            await recorder(tmp_path, http, "http://mock/mcp").introspect()
    message = str(caught.value)
    assert "Streamable HTTP: MCP initialize failed: HTTP 404" in message
    assert "SSE: SSE transport: GET http://mock/mcp returned 410" in message


async def test_a_passing_streamable_http_failure_is_tried_again(tmp_path: Path) -> None:
    app = fake_app(tmp_path / "a", fake_model())
    answers = iter([503])

    async with streaming_client(app) as inner:

        async def flaky(request: httpx2.Request) -> httpx2.Response:
            status = next(answers, None)
            if status is not None:
                return httpx2.Response(status)
            forwarded = await inner.send(
                inner.build_request(
                    request.method,
                    str(request.url),
                    headers=request.headers,
                    content=request.content,
                )
            )
            return httpx2.Response(
                forwarded.status_code, headers=forwarded.headers, content=forwarded.content
            )

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(flaky)) as http:
            rec = recorder(tmp_path, http, "http://fake.test/mcp")
            found = await rec.introspect(probe_versions=False)
    assert found.model.transport.kind == "streamable_http"  # no fall back to SSE


class Scripted:
    """A minimal Streamable HTTP server (JSON replies) whose quirks tests choose."""

    def __init__(
        self,
        *,
        refuse: frozenset[str] = frozenset(),
        sessions: bool = False,
        plain_errors: bool = False,
        report_version: bool = True,
        token: str | None = None,
    ) -> None:
        self.token = token
        self.seen_auth: list[str | None] = []
        self.refuse = refuse
        self.sessions = sessions
        self.plain_errors = plain_errors
        self.report_version = report_version

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE":
            return httpx2.Response(200)
        if request.method != "POST":
            return httpx2.Response(405)
        message = j(jsonfmt.loads(request.content))
        method = message.get("method")
        refused = self._refuse_credentials(request, message)
        if refused is not None:
            return refused
        has_session = bool(request.headers.get(protocol.SESSION_HEADER))
        if self.sessions and method != "initialize" and not has_session:
            return httpx2.Response(400, text="Bad Request: no session")
        if "id" not in message:
            return httpx2.Response(202)
        params = message.get("params") or {}
        if method == "initialize":
            return self._initialize(message["id"], params)
        if self.plain_errors and method != "tools/list":
            return httpx2.Response(500, text="Internal Server Error")
        if method == "tools/list":
            return httpx2.Response(200, json=protocol.result(message["id"], {"tools": [ECHO]}))
        if method == "tools/call":
            return self._tool(request, message["id"], params)
        return httpx2.Response(200, json=protocol.error(message["id"], -32601, "Method not found"))

    def _refuse_credentials(
        self, request: httpx2.Request, message: dict[str, Any]
    ) -> httpx2.Response | None:
        if self.token is None or "id" not in message:
            return None
        sent = request.headers.get("authorization")
        self.seen_auth.append(sent)
        if sent == f"Bearer {self.token}" or request.headers.get("x-api-key") == self.token:
            return None
        challenge = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
        return httpx2.Response(401, headers=challenge, json={"error": "invalid_token"})

    def _initialize(self, request_id: JsonValue, params: dict[str, Any]) -> httpx2.Response:
        if params.get("protocolVersion") in self.refuse:
            reply = protocol.error(request_id, -32602, "Unsupported protocol version")
            return httpx2.Response(200, json=reply)
        result: dict[str, JsonValue] = {"capabilities": {"tools": {}}}
        if self.report_version:
            result["protocolVersion"] = params["protocolVersion"]
        headers = {protocol.SESSION_HEADER: "s-1"} if self.sessions else {}
        body = jsonfmt.dumps(protocol.result(request_id, result)).encode()
        return httpx2.Response(200, headers=headers, content=body)  # no content type

    @staticmethod
    def _tool(
        request: httpx2.Request, request_id: JsonValue, params: dict[str, Any]
    ) -> httpx2.Response:
        name = params.get("name")
        if name == "boom":
            raise httpx2.ReadTimeout("slow tool", request=request)
        if name == "crash":
            return httpx2.Response(500, text="Internal Server Error")
        if name == "silent":
            return httpx2.Response(200)
        arguments: JsonValue = params.get("arguments") or {}
        content: JsonValue = [{"type": "text", "text": jsonfmt.dumps(arguments)}]
        reply = protocol.result(request_id, {"content": content, "echo": arguments})
        return httpx2.Response(200, json=reply)


async def test_version_probes_skip_refusals_and_stop_at_the_budget(tmp_path: Path) -> None:
    server = Scripted(refuse=frozenset({"2024-11-05"}))
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(server)) as http:
        found = await recorder(tmp_path / "a", http, "http://mock/mcp", rps=0).introspect()
        assert found.model.transport.protocol_versions == {
            "2025-03-26": "2025-03-26",
            "2025-06-18": "2025-06-18",
            "2025-11-25": "2025-11-25",
        }
        assert found.model.transport.issues_session is False
        assert found.model.transport.get_stream == "status"
        assert "missing_session" not in found.model.errors
        # 11 calls of introspection (5, and 2 per client capability probed), then one per
        # probed version: the refused 2024-11-05, then 2025-03-26, and the budget is gone
        tight = recorder(tmp_path / "b", http, "http://mock/mcp", rps=0, max_calls=13)
        limited = await tight.introspect()
        assert limited.model.transport.protocol_versions == {"2025-03-26": "2025-03-26"}
        assert tight.calls_made == 13
        # A budget spent while probing capabilities leaves fewer probes, not a failed init
        tighter = recorder(tmp_path / "c", http, "http://mock/mcp", rps=0, max_calls=6)
        assert (await tighter.introspect()).model.transport.protocol_versions == {}


async def test_introspection_budget_runs_out(tmp_path: Path) -> None:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(Scripted())) as http:
        rec = recorder(tmp_path, http, "http://mock/mcp", rps=0, max_calls=2)
        with pytest.raises(RecordingError, match="budget exhausted"):
            await rec.introspect()


# -- recording plans -------------------------------------------------------------------------------


async def test_run_with_approvals_captures_and_privacy(tmp_path: Path) -> None:
    approvals = Approvals(
        approvals=[
            Approval(
                scope="endpoint",
                endpoint="MCP tools/call:whoami",
                max_calls=1,
                approved_at=datetime(2026, 10, 10, tzinfo=UTC),
            )
        ]
    )
    tools = {"echo": ECHO, "delete_item": DELETE_ITEM, "whoami": WHOAMI}
    privacy = RecordingPrivacy(PrivacyConfig(), secret_values=[TOKEN], pseudonym_key="k")
    calls = [
        tool_call(
            "whoami",
            repeat=2,
            capture={"email": "$.result.structuredContent.email", "nothing": "$.result.nope"},
        ),
        tool_call("echo", {"text": "to {{email}}"}, repeat=2, interval_ms=1000, tags=["t"]),
        tool_call("delete_item", {"item_id": 1}),
        tool_call("delete_item", {"item_id": 2}, id="declared-safe", mutating=False),
        {
            "id": "prompt",
            "endpoint": "MCP prompts/get:summarize",
            "request": {"method": "POST", "path": "/mcp"},
        },
    ]
    clock = FakeClock()
    transport = McpTransportModel(response_mode="sse")
    app = fake_app(tmp_path, fake_model(transport=transport))
    async with streaming_client(app) as http:
        rec = McpRecorder(
            RecordingStore(tmp_path / "recordings"),
            "http://fake.test/mcp",
            http,
            headers={"Authorization": f"Bearer {TOKEN}"},
            privacy=privacy,
            approvals=approvals,
            approvals_path=tmp_path / "approvals.json",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        report = await rec.run(plan(*calls), tools)
    whoami, echo, delete, declared, prompt = report.outcomes
    assert len(whoami.exchanges) == 1
    assert whoami.skipped == "approval budget used up after 1 call(s)"
    assert echo.statuses == [200, 200]
    assert delete.skipped == "needs approval (tool may modify state)"
    assert declared.statuses == [200]
    assert prompt.statuses == [200]
    assert report.calls_made == 7  # initialize, 1 + 2 + 1 + 1 calls, the closing DELETE
    # paced at 2 rps; the echo repeat waits its 1 s interval, which is pacing enough
    assert clock.sleeps == [0.5, 0.5, 1.0, 0.5, 0.5, 0.5]
    assert read_model(tmp_path / "approvals.json", Approvals).used == {
        "endpoint:MCP tools/call:whoami:": 1
    }
    store = RecordingStore(tmp_path / "recordings")
    recorded = {e.id: e for _, e in store.iter_exchanges()}
    on_disk = b"".join(p.read_bytes() for p in (tmp_path / "recordings").rglob("*.json"))
    assert TOKEN.encode() not in on_disk
    assert b"jane.roe@example.org" not in on_disk  # pseudonymized, in bodies and SSE events
    first_echo = recorded[echo.exchanges[0]]
    assert first_echo.tags == ["t"]
    assert first_echo.purpose == "sample"
    sent = j(first_echo.request.body.json_value)
    assert sent["params"]["name"] == "echo"  # filled in from the endpoint id
    fake_email = sent["params"]["arguments"]["text"].removeprefix("to ")
    assert fake_email.endswith("@example.com")  # the captured (real) email was sent, then hidden
    assert ("authorization", "Bearer [REDACTED]") in [
        (k.lower(), v) for k, v in first_echo.request.headers
    ]
    who = recorded[whoami.exchanges[0]]
    structured = j(who.response.body.json_value if who.response else None)["result"]
    assert structured["structuredContent"]["api_key"] == "[REDACTED]"
    event = jsonfmt.loads(who.stream[0].data or "")
    assert j(event)["result"]["structuredContent"]["api_key"] == "[REDACTED]"
    assert (
        j(event)["result"]["structuredContent"]["email"] == structured["structuredContent"]["email"]
    )
    assert j(recorded[prompt.exchanges[0]].request.body.json_value)["params"] == {
        "name": "summarize"
    }


async def test_run_without_approvals_or_tool_definitions(tmp_path: Path) -> None:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(Scripted())) as http:
        rec = recorder(tmp_path, http, "http://mock/mcp", rps=0)
        report = await rec.run(
            plan(
                tool_call("echo", {"a": 1}),
                {
                    "id": "list",
                    "endpoint": "MCP tools/list",
                    "request": {"method": "POST", "path": "/mcp"},
                },
            )
        )
    assert report.outcomes[0].skipped == "needs approval (tool may modify state)"
    assert report.outcomes[1].statuses == [200]
    assert not (tmp_path / "approvals.json").exists()


async def test_plans_record_how_the_server_refuses_missing_and_bad_credentials(
    tmp_path: Path,
) -> None:
    server = Scripted(token="good-token")
    safe = {"mutating": False}
    calls = [
        tool_call("echo", {"n": 1}, id="authed", **safe),
        tool_call("echo", {"n": 1}, id="anonymous", **safe),
        tool_call("echo", {"n": 1}, id="bad-token", **safe),
    ]
    calls[1]["request"]["auth"] = "none"
    calls[2]["request"]["auth"] = "invalid"
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(server)) as http:
        rec = recorder(
            tmp_path, http, "http://mock/mcp", rps=0, headers={"Authorization": "Bearer good-token"}
        )
        report = await rec.run(plan(*calls))
    authed, anonymous, bad = report.outcomes
    assert authed.status_text() == "200 ok"
    assert anonymous.status_text() == bad.status_text() == "401 no reply"
    assert server.seen_auth[-2:] == [None, "Bearer fauxcall-invalid-credential"]
    saved = {e.plan_call: e for _, e in RecordingStore(tmp_path / "recordings").iter_exchanges()}
    assert "auth:none" in saved["anonymous"].tags
    assert "auth:invalid" in saved["bad-token"].tags
    assert "good-token" not in saved["bad-token"].model_dump_json()


async def test_auth_probes_need_streamable_http(tmp_path: Path) -> None:
    transport = McpTransportModel(kind="sse", path="/sse", messages_path="/rpc/messages/")
    app = fake_app(tmp_path, fake_model(transport=transport))
    call = tool_call("echo", {"text": "hi"}, mutating=False)
    call["request"]["auth"] = "none"
    async with streaming_client(app) as http:
        report = await recorder(tmp_path, http, "http://fake.test/sse", rps=0).run(plan(call))
    assert report.outcomes[0].error == (
        "RecordingError: auth probes need the Streamable HTTP transport"
    )


RESOURCE_DOC: JsonValue = {
    "resource": "http://mock/mcp",
    "authorization_servers": ["https://auth.mock/"],
    "scopes_supported": ["read", "write"],
}
SERVER_DOC: JsonValue = {"issuer": "https://auth.mock", "token_endpoint": "https://auth.mock/t"}


def oauth_server(
    *, challenge: bool = True, documents: bool = True, openid: bool = False
) -> Callable[[httpx2.Request], httpx2.Response]:
    """The scripted MCP server behind a token, publishing (or not) OAuth discovery documents."""
    server = Scripted(token="good-token")
    named = 'Bearer resource_metadata="http://mock/.well-known/oauth-protected-resource/mcp"'

    def answer(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/.well-known/"):
            assert "authorization" not in request.headers  # public documents: no credentials
            if not documents:
                return httpx2.Response(404)
            if path == "/.well-known/oauth-protected-resource/mcp":
                return httpx2.Response(200, json=RESOURCE_DOC)
            if path == "/.well-known/oauth-authorization-server" and not openid:
                return httpx2.Response(200, json=SERVER_DOC)
            if path == "/.well-known/openid-configuration" and openid:
                return httpx2.Response(200, json=SERVER_DOC)
            return httpx2.Response(404)
        response = server(request)
        if response.status_code == 401 and not challenge:
            del response.headers["www-authenticate"]
        elif response.status_code == 401:
            response.headers["www-authenticate"] = named
        return response

    return answer


@pytest.mark.parametrize("openid", [False, True])
async def test_introspection_learns_how_the_server_refuses_and_its_oauth(
    tmp_path: Path, openid: bool
) -> None:
    transport = httpx2.MockTransport(oauth_server(openid=openid))
    async with httpx2.AsyncClient(transport=transport, base_url="https://auth.mock") as http:
        rec = recorder(
            tmp_path, http, "http://mock/mcp", rps=0, headers={"Authorization": "Bearer good-token"}
        )
        found = await rec.introspect(probe_versions=False)
    assert found.auth is not None
    assert [scheme.kind for scheme in found.auth.schemes] == ["bearer", "oauth2"]
    oauth = found.auth.schemes[1].oauth
    assert oauth is not None
    assert oauth.protected_resource_metadata == RESOURCE_DOC
    assert oauth.authorization_server_metadata == SERVER_DOC
    assert oauth.scopes == ["read", "write"]
    assert (found.auth.missing_template, found.auth.invalid_template) == ("unauthorized",) * 2
    error = found.auth_error
    assert error is not None
    assert (error.status, error.body_json, error.headers) == (401, {"error": "invalid_token"}, {})
    assert "refuses calls without credentials (HTTP 401): bearer and oauth2" in found.notes
    probe = [e for _, e in RecordingStore(tmp_path / "recordings").iter_exchanges()][-1]
    assert probe.endpoint == "MCP initialize:no-credentials"
    assert (probe.purpose, probe.tags) == ("auth", ["auth:none"])


async def test_without_oauth_documents_the_challenge_is_kept(tmp_path: Path) -> None:
    transport = httpx2.MockTransport(oauth_server(documents=False))
    async with httpx2.AsyncClient(transport=transport) as http:
        found = await recorder(
            tmp_path, http, "http://mock/mcp", rps=0, headers={"X-Api-Key": "good-token"}
        ).introspect(probe_versions=False)
    assert found.auth is not None
    (scheme,) = found.auth.schemes  # a key in a header, no OAuth
    assert (scheme.kind, scheme.location, scheme.name) == ("api_key", "header", "X-Api-Key")
    assert found.auth_error is not None
    assert "resource_metadata" in found.auth_error.headers["WWW-Authenticate"]


async def test_auth_probe_edge_cases(tmp_path: Path) -> None:
    transport = httpx2.MockTransport(oauth_server(challenge=False, documents=False))
    async with httpx2.AsyncClient(transport=transport) as http:
        found = await recorder(
            tmp_path, http, "http://mock/mcp", rps=0, headers={"X-Api-Key": "good-token"}
        ).introspect(probe_versions=False)
    assert found.auth_error is not None
    assert found.auth_error.headers == {}  # no challenge to repeat
    open_server = Scripted()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(open_server)) as http:
        found = await recorder(
            tmp_path / "b", http, "http://mock/mcp", rps=0, headers={"Authorization": "Bearer x"}
        ).introspect(probe_versions=False)
    assert found.auth is None
    assert "answers without credentials too (HTTP 200)" in found.notes
    assert _static_scheme({"Authorization": "Basic abc"}).kind == "basic"


@pytest.mark.parametrize("failure", ["unreachable", "not json", "a list"])
async def test_unreadable_oauth_documents_mean_no_oauth(tmp_path: Path, failure: str) -> None:
    serve = oauth_server()

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path.startswith("/.well-known/"):
            if failure == "unreachable":
                raise httpx2.ConnectError("down", request=request)
            return httpx2.Response(200, text="<html>" if failure == "not json" else "[1]")
        return serve(request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as http:
        found = await recorder(
            tmp_path, http, "http://mock/mcp", rps=0, headers={"Authorization": "Bearer good-token"}
        ).introspect(probe_versions=False)
    assert found.auth is not None
    assert [scheme.kind for scheme in found.auth.schemes] == ["bearer"]


async def test_a_tool_call_that_never_left_gives_its_approval_back(tmp_path: Path) -> None:
    server = Scripted()

    def answer(request: httpx2.Request) -> httpx2.Response:
        if b'"delete_item"' in request.content or b'"down"' in request.content:
            raise httpx2.ConnectError("connection refused", request=request)
        return server(request)

    call = tool_call("delete_item", {"id": 1})
    planned = PlannedCall.model_validate(call)
    approvals = Approvals(
        approvals=[
            Approval(
                scope="call",
                endpoint=planned.endpoint,
                fingerprint=planned.fingerprint(),
                max_calls=1,
                approved_at=datetime.now(UTC),
            )
        ]
    )
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as http:
        rec = recorder(
            tmp_path,
            http,
            "http://mock/mcp",
            rps=0,
            approvals=approvals,
            approvals_path=tmp_path / "approvals.json",
        )
        read = tool_call("echo", {"text": "down"}, id="read", mutating=False)
        report = await rec.run(plan(call, read), {"delete_item": DELETE_ITEM})
    assert report.outcomes[0].error == "ConnectError: connection refused"
    assert report.outcomes[1].error == "ConnectError: connection refused"  # nothing to give back
    assert set(read_model(tmp_path / "approvals.json", Approvals).used.values()) == {0}


async def test_run_errors_budget_and_odd_replies(tmp_path: Path) -> None:
    server = Scripted()
    safe = {"mutating": False}
    calls = [
        tool_call("boom", {}, **safe),
        tool_call("crash", {}, id="crash", **safe),
        tool_call("silent", {}, id="silent", **safe),
        tool_call("echo", {"n": jsonfmt.loads("1.50")}, id="echo", **safe),
        tool_call("echo", {}, id="over-budget", **safe),
        tool_call("echo", {}, id="never", **safe),
    ]
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(server)) as http:
        rec = recorder(tmp_path, http, "http://mock/mcp?key=abcdef", rps=0, max_calls=5)
        report = await rec.run(plan(*calls))
    boom, crash, silent, echo, over, never = report.outcomes
    assert boom.error == "ReadTimeout: slow tool"
    assert crash.statuses == [500]
    assert silent.statuses == [200]
    assert echo.statuses == [200]
    assert [o.answers for o in (crash, silent, echo)] == [["no reply"], ["no reply"], ["ok"]]
    assert echo.status_text() == "200 ok"
    assert (over.skipped, never.skipped) == ("budget exhausted", "budget exhausted")
    assert report.budget_exhausted
    assert report.calls_made == 5  # this server has no sessions, so nothing to DELETE
    recorded = {e.plan_call: e for _, e in RecordingStore(tmp_path / "recordings").iter_exchanges()}
    crashed = recorded["crash"].response
    assert crashed is not None
    assert (crashed.body.kind, crashed.body.text) == ("text", "Internal Server Error")
    quiet = recorded["silent"].response
    assert quiet is not None
    assert quiet.body.kind == "empty"
    echoed = recorded["echo"]
    assert echoed.request.query == [("key", "abcdef")]
    assert echoed.response is not None
    sent = j(echoed.request.body.json_value)["params"]["arguments"]
    assert jsonfmt.dumps(sent) == '{"n":1.50}'  # the plan's spelling went out on the wire


async def test_introspect_odd_servers(tmp_path: Path) -> None:
    server = Scripted(sessions=True, plain_errors=True, report_version=False)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(server)) as http:
        found = await recorder(tmp_path, http, "http://mock/mcp", rps=0).introspect()
    learned = found.model.transport
    assert learned.issues_session
    assert (learned.missing_session_status, learned.requires_session) == (400, True)
    assert learned.default_protocol_version == protocol.LATEST_HANDSHAKE_VERSION
    assert (learned.content_type_json, learned.content_type_sse) == (
        "application/json",
        "text/event-stream",
    )  # the server sent no content type: defaults are kept
    assert learned.protocol_versions == {}  # it never says which version it speaks
    assert found.model.errors == {}  # plain-text errors aren't JSON-RPC: nothing to replay
    recorded = {e.endpoint: e for _, e in RecordingStore(tmp_path / "recordings").iter_exchanges()}
    missing = recorded["MCP tools/list:missing-session"].response
    assert missing is not None
    assert (missing.status, missing.body.kind) == (400, "text")


def _delete_budget(max_calls: int) -> Approvals:
    approval = Approval(
        scope="endpoint",
        endpoint="MCP tools/call:delete_item",
        max_calls=max_calls,
        approved_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    return Approvals(approvals=[approval])


async def test_a_budget_spent_elsewhere_while_pacing_stops_the_tool_call(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"

    class SpendingClock(FakeClock):
        async def sleep(self, seconds: float) -> None:
            await super().sleep(seconds)
            if path.exists():  # another run spends the rest of the budget meanwhile
                current = read_model(path, Approvals)
                current.used = {"endpoint:MCP tools/call:delete_item:": 2}
                write_model(path, current)

    app = fake_app(tmp_path, fake_model())
    async with streaming_client(app) as http:
        rec = recorder(
            tmp_path,
            http,
            "http://fake.test/mcp",
            SpendingClock(),
            approvals=_delete_budget(2),
            approvals_path=path,
        )
        report = await rec.run(
            plan(tool_call("delete_item", {"item_id": 1}, repeat=2)),
            {"delete_item": DELETE_ITEM},
        )
    assert report.outcomes[0].skipped == "approval budget used up after 1 call(s)"
    assert len(report.outcomes[0].exchanges) == 1


async def test_tool_budgets_without_an_approvals_file(tmp_path: Path) -> None:
    app = fake_app(tmp_path, fake_model())
    approvals = _delete_budget(1)
    async with streaming_client(app) as http:
        rec = recorder(tmp_path, http, "http://fake.test/mcp", approvals=approvals)
        report = await rec.run(
            plan(tool_call("delete_item", {"item_id": 1}, repeat=2)),
            {"delete_item": DELETE_ITEM},
        )
    assert report.outcomes[0].skipped == "approval budget used up after 1 call(s)"
    assert rec.approvals is not None
    assert rec.approvals.used == {"endpoint:MCP tools/call:delete_item:": 1}
    assert not (tmp_path / "approvals.json").exists()
