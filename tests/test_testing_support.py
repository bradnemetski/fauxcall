from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from fauxcall.config import FakeSettings
from fauxcall.model.api import ApiModel, RealTarget, SourceInfo
from fauxcall.model.auth import AuthModel, AuthScheme
from fauxcall.model.endpoints import EndpointModel, StreamModel
from fauxcall.model.mcp import McpModel, McpToolModel, McpTransportModel
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange
from fauxcall.privacy.pseudonym import Pseudonymizer
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime import FakeRuntime, create_app, register_tool
from fauxcall.storage import write_model
from fauxcall.testing.client import PLACEHOLDER_KEY, PLACEHOLDER_TOKEN
from fauxcall.testing.support import (
    REPLAY_PURPOSES,
    STATEFUL_ROLES,
    RecordedCase,
    ReplayOutcome,
    fake_under_test,
    recorded_cases,
    replay_against,
)
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.model.common import Purpose
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

FETCHED = datetime(2026, 10, 10, tzinfo=UTC)
KEY = "pseudonym-key-for-tests-0123456789"


def rest_model(*, base_url: str = "https://petstore.example.com") -> ApiModel:
    return ApiModel(
        name="petstore",
        title="Petstore",
        source=SourceInfo(kind="openapi", url=f"{base_url}/openapi.json", fetched_at=FETCHED),
        real=RealTarget(base_url=base_url),
        endpoints=[
            EndpointModel(id="GET /pets/{id}", method=HttpMethod.GET, path="/pets/{id}"),
            EndpointModel(id="GET /text", method=HttpMethod.GET, path="/text"),
            EndpointModel(id="GET /empty", method=HttpMethod.GET, path="/empty"),
            EndpointModel(id="POST /echo", method=HttpMethod.POST, path="/echo", role="echo"),
            EndpointModel(
                id="POST /pets",
                method=HttpMethod.POST,
                path="/pets",
                role="create",
                collection="pets",
            ),
            EndpointModel(
                id="GET /events",
                method=HttpMethod.GET,
                path="/events",
                role="sse",
                stream=StreamModel(kind="sse"),
            ),
        ],
    )


ADD: JsonObject = {
    "name": "add",
    "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}}},
    "annotations": {"readOnlyHint": True},
}


def mcp_model(path: str = "/mcp") -> ApiModel:
    return ApiModel(
        name="tools",
        title="Tools",
        kind="mcp",
        source=SourceInfo(kind="mcp", url=f"https://mcp.example.com{path}", fetched_at=FETCHED),
        real=RealTarget(base_url="https://mcp.example.com"),
        mcp=McpModel(
            transport=McpTransportModel(path=path),
            initialize_result={
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tools", "version": "1.0.0"},
            },
            tools=[McpToolModel(definition=ADD, behavior="computed")],
        ),
    )


def mcp_fake(data_dir: Path) -> FastAPI:
    settings = FakeSettings(name="tools", data_dir=data_dir, fast=True, log_level="WARNING")
    app = create_app(settings, runtime=FakeRuntime(settings, mcp_model()), configure_logs=False)

    async def add(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        total = cast("float", arguments["a"]) + cast("float", arguments.get("b", 0))
        return {"content": [{"type": "text", "text": str(total)}], "isError": False}

    register_tool(app, "add", add)
    return app


def rest_fake(seen: list[dict[str, Any]] | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/pets/{pet_id}")
    async def get_pet(pet_id: int) -> Response:
        return JSONResponse({"id": pet_id, "name": "Rex"})

    @app.get("/text")
    async def text() -> Response:
        return PlainTextResponse("hello")

    @app.get("/empty")
    async def empty() -> Response:
        return Response(status_code=200)

    @app.post("/echo")
    async def echo(request: Request) -> Response:
        raw = await request.body()
        if seen is not None:
            seen.append(
                {
                    "body": raw.decode(),
                    "host": request.headers.get("host"),
                    "length": request.headers.get("content-length"),
                    "type": request.headers.get("content-type"),
                    "trace": request.headers.get("x-trace"),
                    "query": request.query_params.multi_items(),
                }
            )
        return JSONResponse({"echoed": True})

    return app


def exchange(
    exchange_id: str,
    endpoint: str,
    *,
    purpose: Purpose = "sample",
    method: str = "GET",
    path: str = "/pets/1",
    query: list[tuple[str, str]] | None = None,
    headers: list[tuple[str, str]] | None = None,
    request_body: Body | None = None,
    status: int | None = 200,
    response_body: Body | None = None,
) -> Exchange:
    return Exchange(
        id=exchange_id,
        endpoint=endpoint,
        purpose=purpose,
        started_at=FETCHED,
        started_ns=int(FETCHED.timestamp()) * 1_000_000_000,
        request=CapturedRequest(
            method=HttpMethod(method),
            url=f"https://petstore.example.com{path}",
            path=path,
            query=query or [],
            headers=headers or [],
            body=request_body or Body(),
        ),
        response=None
        if status is None
        else CapturedResponse(status=status, body=response_body or Body()),
    )


def json_body(value: JsonValue) -> Body:
    return Body(kind="json", content_type="application/json", json_value=value)


def case(exchange_: Exchange, *, mcp: bool = False) -> RecordedCase:
    return RecordedCase(id=f"{exchange_.id}-{exchange_.endpoint}", exchange=exchange_, mcp=mcp)


# -- fake_under_test -------------------------------------------------------------------------------


def test_fake_under_test_reads_the_fakes_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_model(tmp_path / "data" / "model.json", rest_model())
    monkeypatch.setenv("FAUXCALL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FAUXCALL_PSEUDONYM_KEY", KEY)
    monkeypatch.setenv("FAUXCALL_REAL__HEADERS", '{"X-Api-Key": "s3cret"}')
    monkeypatch.setenv("FAUXCALL_REAL__QUERY", '{"token": "t0ken"}')

    class Settings(FakeSettings):
        """A generated fake's settings class."""

    built: list[Settings] = []

    def build_app(settings: Settings) -> FastAPI:
        built.append(settings)
        return FastAPI()

    fake = fake_under_test(Settings, build_app)
    assert fake.model is not None
    assert fake.model.name == "petstore"
    assert fake.real_base_url == "https://petstore.example.com"
    assert fake.real_headers == {"X-Api-Key": "s3cret"}
    assert fake.real_query == {"token": "t0ken"}
    assert fake.reports_dir == tmp_path / "data" / "reports"
    assert fake.mcp_path == "/mcp"
    assert fake.real_transport is None
    assert fake.translate is not None
    expected = Pseudonymizer(KEY).pseudonym("jane@corp.com", "email")
    assert fake.translate("jane@corp.com", "email") == expected
    assert isinstance(fake.app_factory(), FastAPI)
    assert built[0].fast
    assert built[0].log_level == "WARNING"


def test_fake_under_test_without_a_key_and_with_a_real_url_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_model(tmp_path / "data" / "model.json", mcp_model("/v1/mcp"))
    monkeypatch.setenv("FAUXCALL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FAUXCALL_REAL__BASE_URL", "https://staging.example.com")
    monkeypatch.delenv("FAUXCALL_PSEUDONYM_KEY", raising=False)

    class Settings(FakeSettings):
        """Settings without a pseudonym key."""

    fake = fake_under_test(Settings, lambda _settings: FastAPI())
    assert fake.translate is None
    assert fake.real_base_url == "https://staging.example.com"
    assert fake.real_headers == {}
    assert fake.mcp_path == "/v1/mcp"


# -- recorded_cases --------------------------------------------------------------------------------


def test_no_model_no_cases(tmp_path: Path) -> None:
    assert recorded_cases(tmp_path) == []


def test_recorded_cases_keep_stateless_read_like_requests(tmp_path: Path) -> None:
    write_model(tmp_path / "model.json", rest_model())
    store = RecordingStore(tmp_path / "recordings")
    multipart = Body(kind="multipart", content_type="multipart/form-data")
    binary = Body(kind="binary", content_type="application/octet-stream")
    for recorded in [
        exchange("000001", "GET /pets/{id}"),
        exchange("000002", "GET /pets/{id}", purpose="discovery"),
        exchange("000003", "GET /pets/{id}", status=None),
        exchange("000004", "POST /pets", method="POST", path="/pets"),
        exchange("000005", "GET /events", path="/events"),
        exchange("000006", "GET /unknown", path="/unknown"),
        exchange("000007", "POST /echo", method="POST", path="/echo", request_body=multipart),
        exchange("000008", "POST /echo", method="POST", path="/echo", request_body=binary),
        exchange("000009", "MCP tools/call:add", method="POST", path="/mcp"),
        exchange("000010", "MCP initialize", method="POST", path="/mcp"),
        exchange("000011", "MCP resources/read:file:///a", method="POST", path="/mcp"),
        exchange("000012", "GET /text", path="/text", purpose="invalid"),
    ]:
        store.save(recorded)
    cases = recorded_cases(tmp_path)
    assert [(c.id, c.mcp) for c in cases] == [
        ("000001-GET /pets/{id}", False),
        ("000009-MCP tools/call:add", True),
        ("000011-MCP resources/read:file:///a", True),
        ("000012-GET /text", False),
    ]
    assert cases[0].exchange.id == "000001"
    narrowed = recorded_cases(
        tmp_path, purposes=frozenset({"sample"}), exclude_endpoints=frozenset({"GET /pets/{id}"})
    )
    assert [c.id for c in narrowed] == [
        "000009-MCP tools/call:add",
        "000011-MCP resources/read:file:///a",
    ]
    assert "sample" in REPLAY_PURPOSES
    assert {"create", "sse", "websocket"} <= STATEFUL_ROLES


# -- replay_against (REST) -------------------------------------------------------------------------


async def test_replay_matches_status_and_shape() -> None:
    recorded = exchange("000001", "GET /pets/{id}", response_body=json_body({"id": 1, "name": "x"}))
    outcome = await replay_against(rest_fake(), case(recorded))
    assert outcome.ok
    assert (outcome.expected_status, outcome.actual_status) == (200, 200)


async def test_replay_reports_status_and_shape_differences() -> None:
    recorded = exchange(
        "000002",
        "GET /pets/{id}",
        status=404,
        response_body=json_body({"id": 1, "name": "x", "age": 3, "a": 1, "b": 2, "c": 3, "d": 4}),
    )
    outcome = await replay_against(rest_fake(), case(recorded))
    assert not outcome.ok
    assert outcome.problems[0] == "status 200, recorded 404"
    assert outcome.problems[1] == "shape: $.age: missing in fake"
    assert len(outcome.problems) == 6  # at most five shape problems
    assert outcome.explain().startswith(
        "GET /pets/{id} (000002, GET /pets/1):\n  - status 200, recorded 404\n  - shape:"
    )


async def test_replay_reports_non_json_and_empty_answers() -> None:
    text = exchange("000003", "GET /text", path="/text", response_body=json_body({"a": 1}))
    outcome = await replay_against(rest_fake(), case(text))
    assert outcome.problems == ["recorded a JSON body, the fake answered non-JSON"]
    empty = exchange("000004", "GET /empty", path="/empty", response_body=json_body({"a": 1}))
    outcome = await replay_against(rest_fake(), case(empty))
    assert outcome.problems == ["shape: $: object vs null"]
    plain = exchange(
        "000005",
        "GET /text",
        path="/text",
        response_body=Body(kind="text", content_type="text/plain", text="other words"),
    )
    assert (await replay_against(rest_fake(), case(plain))).ok


@pytest.mark.parametrize(
    ("body", "sent", "content_type"),
    [
        (json_body({"name": "Rex"}), '{"name":"Rex"}', "application/json"),
        (Body(kind="text", content_type="text/plain", text="hi"), "hi", "text/plain"),
        (
            Body(kind="form", content_type="application/x-www-form-urlencoded", text="a=1"),
            "a=1",
            "application/x-www-form-urlencoded",
        ),
        (Body(kind="text", content_type="text/plain"), "", "text/plain"),
        (Body(), "", None),
    ],
)
async def test_replay_sends_the_recorded_request(
    body: Body, sent: str, content_type: str | None
) -> None:
    headers = [("Host", "petstore.example.com"), ("Content-Length", "99"), ("X-Trace", "t1")]
    if content_type:
        headers.append(("Content-Type", content_type))
    recorded = exchange(
        "000006",
        "POST /echo",
        method="POST",
        path="/echo",
        query=[("q", "1"), ("q", "2")],
        headers=headers,
        request_body=body,
        response_body=json_body({"echoed": True}),
    )
    seen: list[dict[str, Any]] = []
    outcome = await replay_against(rest_fake(seen), case(recorded))
    assert outcome.ok, outcome.explain()
    assert seen == [
        {
            "body": sent,
            "host": "fake.test",
            "length": str(len(sent)),  # recomputed, never the recorded 99
            "type": content_type,
            "trace": "t1",
            "query": [("q", "1"), ("q", "2")],
        }
    ]


# -- replay_against (MCP) --------------------------------------------------------------------------


def rpc(
    exchange_id: str, *, request: JsonValue, response: JsonValue, status: int = 200
) -> Exchange:
    return exchange(
        exchange_id,
        "MCP tools/call:add",
        method="POST",
        path="/mcp",
        request_body=json_body(request),
        status=status,
        response_body=json_body(response),
    )


CALL: JsonObject = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/call",
    "params": {"name": "add", "arguments": {"a": 1, "b": 2}},
}
RESULT: JsonObject = {
    "jsonrpc": "2.0",
    "id": 7,
    "result": {"content": [{"type": "text", "text": "3"}], "isError": False},
}


async def test_mcp_replay_matches(tmp_path: Path) -> None:
    outcome = await replay_against(
        mcp_fake(tmp_path), case(rpc("1", request=CALL, response=RESULT), mcp=True)
    )
    assert outcome.ok, outcome.explain()
    assert outcome.actual_status == 200


async def test_mcp_replay_reports_shape_and_member_differences(tmp_path: Path) -> None:
    richer: JsonObject = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"content": [], "isError": False, "structuredContent": {"sum": 3}},
    }
    outcome = await replay_against(
        mcp_fake(tmp_path), case(rpc("2", request=CALL, response=richer), mcp=True)
    )
    assert outcome.problems == ["shape: $.structuredContent: missing in fake"]
    failed: JsonObject = {"jsonrpc": "2.0", "id": 7, "error": {"code": -32602, "message": "bad"}}
    outcome = await replay_against(
        mcp_fake(tmp_path), case(rpc("3", request=CALL, response=failed), mcp=True)
    )
    assert outcome.problems == [
        "recorded has 'result': False, fake: True",
        "recorded has 'error': True, fake: False",
    ]


async def test_mcp_replay_reports_status_and_tolerates_odd_recordings(tmp_path: Path) -> None:
    accepted = rpc("4", request=CALL, response=RESULT, status=202)
    outcome = await replay_against(mcp_fake(tmp_path), case(accepted, mcp=True))
    assert outcome.problems == ["status 200, recorded 202"]
    odd = exchange(
        "5",
        "MCP tools/call:add",
        method="POST",
        path="/mcp",
        request_body=Body(kind="text", content_type="text/plain", text="not json"),
        response_body=Body(kind="text", content_type="text/plain", text="nope"),
    )
    outcome = await replay_against(mcp_fake(tmp_path), case(odd, mcp=True))
    assert outcome.ok  # nothing structured to compare


async def test_mcp_replay_when_the_fake_does_not_speak_mcp() -> None:
    outcome = await replay_against(
        rest_fake(), case(rpc("6", request=CALL, response=RESULT), mcp=True)
    )
    assert outcome.actual_status is None
    assert len(outcome.problems) == 1
    assert outcome.problems[0].startswith("MCP initialize failed: HTTP 404")


def test_outcome_explains_every_problem() -> None:
    recorded = exchange("000007", "GET /pets/{id}")
    outcome = ReplayOutcome(case(recorded), 200, 500, ["status 500, recorded 200", "shape: x"])
    assert outcome.explain() == (
        "GET /pets/{id} (000007, GET /pets/1):\n  - status 500, recorded 200\n  - shape: x"
    )
    assert not outcome.ok


# -- credentials in replays ------------------------------------------------------------------------

AUTH = AuthModel(
    schemes=[
        AuthScheme(kind="api_key", location="header", name="X-Api-Key"),
        AuthScheme(kind="api_key", location="query", name="token"),
        AuthScheme(kind="api_key", location="cookie", name="sid"),
        AuthScheme(kind="bearer"),
    ]
)
REDACTED_HEADERS = [
    ("X-Api-Key", "[REDACTED]"),
    ("Authorization", "Bearer [REDACTED]"),
    ("Cookie", "sid=[REDACTED]"),
    ("Accept", "application/json"),
]


def secure_fake(seen: list[dict[str, str | None]]) -> FastAPI:
    app = FastAPI()

    @app.get("/secure")
    async def secure(request: Request) -> Response:
        credentials = {
            "key": request.headers.get("x-api-key"),
            "authorization": request.headers.get("authorization"),
            "token": request.query_params.get("token"),
            "sid": request.cookies.get("sid"),
        }
        seen.append({**credentials, "q": request.query_params.get("q")})
        if not any(credentials.values()):
            return JSONResponse({"error": "missing"}, status_code=401)
        return JSONResponse({"ok": True})

    return app


def secure_exchange(status: int, body: JsonValue, *, purpose: Purpose = "sample") -> Exchange:
    return exchange(
        "000020",
        "GET /secure",
        purpose=purpose,
        path="/secure",
        query=[("token", "[REDACTED]"), ("q", "1")],
        headers=REDACTED_HEADERS,
        status=status,
        response_body=json_body(body),
    )


async def test_replays_swap_redacted_credentials_for_placeholders() -> None:
    seen: list[dict[str, str | None]] = []
    recorded = RecordedCase("c", secure_exchange(200, {"ok": True}), mcp=False, auth=AUTH)
    outcome = await replay_against(secure_fake(seen), recorded)
    assert outcome.ok, outcome.explain()
    assert seen == [
        {
            "key": PLACEHOLDER_KEY,
            "authorization": f"Bearer {PLACEHOLDER_TOKEN}",
            "token": PLACEHOLDER_KEY,
            "sid": PLACEHOLDER_KEY,
            "q": "1",
        }
    ]


@pytest.mark.parametrize(("status", "purpose"), [(401, "sample"), (401, "auth"), (403, "auth")])
async def test_auth_probes_are_replayed_without_credentials(status: int, purpose: Purpose) -> None:
    seen: list[dict[str, str | None]] = []
    probe = secure_exchange(status, {"error": "missing"}, purpose=purpose)
    outcome = await replay_against(
        secure_fake(seen), RecordedCase("c", probe, mcp=False, auth=AUTH)
    )
    assert seen == [{"key": None, "authorization": None, "token": None, "sid": None, "q": "1"}]
    assert outcome.problems == ([] if status == 401 else ["status 401, recorded 403"])


async def test_without_auth_schemes_recorded_headers_are_sent_as_recorded() -> None:
    seen: list[dict[str, str | None]] = []
    outcome = await replay_against(secure_fake(seen), case(secure_exchange(200, {"ok": True})))
    assert outcome.ok
    assert seen[0]["authorization"] == "Bearer [REDACTED]"
    assert seen[0]["token"] == "[REDACTED]"


def test_recorded_cases_carry_the_auth_schemes(tmp_path: Path) -> None:
    model = rest_model().model_copy(update={"auth": AUTH})
    write_model(tmp_path / "model.json", model)
    RecordingStore(tmp_path / "recordings").save(exchange("000001", "GET /pets/{id}"))
    (only,) = recorded_cases(tmp_path)
    assert only.auth == AUTH


async def test_mcp_replays_authenticate_with_placeholders(tmp_path: Path) -> None:
    settings = FakeSettings(name="tools", data_dir=tmp_path, fast=True, log_level="WARNING")
    model = mcp_model().model_copy(update={"auth": AuthModel(schemes=[AuthScheme(kind="bearer")])})
    app = create_app(settings, runtime=FakeRuntime(settings, model), configure_logs=False)

    async def add(_ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        return {"content": [{"type": "text", "text": str(arguments["a"])}], "isError": False}

    register_tool(app, "add", add)
    recorded = rpc("8", request=CALL, response=RESULT)
    with_auth = await replay_against(app, RecordedCase("c", recorded, mcp=True, auth=model.auth))
    assert with_auth.ok, with_auth.explain()
    without = await replay_against(app, RecordedCase("c", recorded, mcp=True))
    assert without.problems[0].startswith("MCP initialize failed: HTTP 401")
