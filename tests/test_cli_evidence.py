"""CLI: plans and approvals, recording (REST and MCP), sql, and the analyze commands."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from typer.testing import CliRunner

from fauxcall.cli import common
from fauxcall.cli.evidence import _example
from fauxcall.cli.main import app
from fauxcall.config import FakeSettings
from fauxcall.model.api import ApiModel, RealTarget, RoutingModel, SourceInfo
from fauxcall.model.endpoints import EndpointModel, ParamModel, RequestBodyModel, ResponseVariant
from fauxcall.model.mcp import McpModel, McpToolModel, McpTransportModel
from fauxcall.model.plan import Approvals, RecordingPlan
from fauxcall.model.progress import Progress
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange, StreamEvent
from fauxcall.model.world import CollectionModel, WorldRecord
from fauxcall.paths import Workspace
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime.app import FakeRuntime, create_app
from fauxcall.storage import read_model, write_model
from fauxcall.types import HttpMethod
from fauxcall.world.io import save_collection

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import Result

    from fauxcall.paths import FakeLayout
    from fauxcall.types import JsonValue

KEY = "test-pseudonym-key-0123456789abcdef"
BASE = "https://shop.test"
runner = CliRunner()

TOKEN = "sk-live-0123456789abcdef"
API_KEY = "hdr-key-9876543210zz"
QUERY_KEY = "qry-key-1122334455yy"
USER_KEY = "alice-api-key-24680xx"
USER_TOKEN = "alice-bearer-13579ww"
PASSWORD = "alice-password-97531vv"
JEV = "jev-key-1122334455uu"


# -- helpers --------------------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    for variable in list(os.environ):
        if variable.startswith("FAUXCALL_") or variable.endswith("TYPESAFE_API_KEY"):
            monkeypatch.delenv(variable)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAUXCALL_WORKSPACE", str(tmp_path / "fakes"))
    monkeypatch.setenv("FAUXCALL_RECORD_RPS", "10000")
    return Workspace(tmp_path / "fakes")


def invoke(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == code, (result.output, result.exception)
    return result


def serve_with(monkeypatch: pytest.MonkeyPatch, target: FastAPI) -> None:
    def client(*, follow_redirects: bool = True) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=target), follow_redirects=follow_redirects
        )

    monkeypatch.setattr(common, "http_client", client)


def real_api() -> FastAPI:
    """A small 'real' API: items, users, a job that finishes on the third poll, an echo."""
    api = FastAPI()
    started = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    items: dict[int, dict[str, Any]] = {
        1: {"id": 1, "name": "Widget", "owner_id": 10, "price": 2.50},
        2: {"id": 2, "name": "Gadget", "owner_id": 11, "price": 10.00},
        3: {"id": 3, "name": "Doohickey", "owner_id": 10, "price": 1.25},
    }
    polls: dict[str, int] = {}

    def stamped(item: dict[str, Any]) -> dict[str, Any]:
        return {**item, "updated_at": datetime.now(UTC).isoformat(timespec="seconds")}

    @api.get("/v1/items")
    async def list_items() -> list[dict[str, Any]]:
        return [stamped(item) for item in items.values()]

    @api.get("/v1/items/{item_id}")
    async def get_item(item_id: int) -> JSONResponse:
        if item_id not in items:
            return JSONResponse({"error": "not found", "id": item_id}, status_code=404)
        return JSONResponse(stamped(items[item_id]))

    @api.post("/v1/items", status_code=201)
    async def create_item(request: Request) -> dict[str, Any]:
        body = await request.json()
        new = {"id": max(items) + 1, **body, "created_at": started.isoformat()}
        items[new["id"]] = new
        return new

    @api.get("/v1/users")
    async def users() -> list[dict[str, Any]]:
        return [{"id": 10, "handle": "u10"}, {"id": 11, "handle": "u11"}]

    @api.post("/v1/jobs", status_code=202)
    async def submit() -> dict[str, str]:
        polls["j1"] = 0
        return {"id": "j1", "state": "queued"}

    @api.get("/v1/jobs/{job_id}")
    async def job(job_id: str) -> dict[str, str]:
        polls[job_id] = polls.get(job_id, 0) + 1
        state = {1: "queued", 2: "running"}.get(polls[job_id], "done")
        return {"id": job_id, "state": state}

    @api.get("/v1/echo")
    async def echo(request: Request) -> JSONResponse:
        payload = {
            "headers": dict(request.headers),
            "query": dict(request.query_params),
            "url": str(request.url),
            "account": {"note": f"keys {USER_KEY} {USER_TOKEN} {PASSWORD} {JEV}"},
        }
        return JSONResponse(payload, headers={"x-debug": f"{TOKEN} {USER_TOKEN}"})

    return api


def endpoint(method: str, path: str, **extra: Any) -> EndpointModel:
    return EndpointModel.model_validate(
        {"id": f"{method} {path}", "method": method, "path": path, **extra}
    )


def shop_model() -> ApiModel:
    item_schema: JsonValue = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
        "required": ["id", "name"],
    }
    return ApiModel(
        name="shop",
        title="Shop",
        source=SourceInfo(kind="openapi", url=f"{BASE}/openapi.json", fetched_at=datetime.now(UTC)),
        real=RealTarget(base_url=BASE, base_path="/v1"),
        endpoints=[
            endpoint(
                "GET",
                "/v1/items",
                params=[
                    {
                        "name": "limit",
                        "location": "query",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
            ),
            endpoint(
                "GET",
                "/v1/items/{id}",
                params=[
                    ParamModel(
                        name="id",
                        location="path",
                        required=True,
                        param_schema={"type": "integer", "example": 7},
                    ),
                    ParamModel(name="verbose", location="query", param_schema={"type": "boolean"}),
                ],
                responses=[
                    ResponseVariant(status=200, documented_schema=item_schema),
                    ResponseVariant(status=404, documented_schema={"type": "object"}),
                ],
            ),
            endpoint(
                "POST",
                "/v1/items",
                request_body=RequestBodyModel(),
                responses=[ResponseVariant(status=201)],
            ),
            endpoint("GET", "/v1/users"),
            endpoint("POST", "/v1/jobs"),
            endpoint("GET", "/v1/jobs/{id}"),
            endpoint("GET", "/v1/echo"),
        ],
    )


def make_fake(
    ws: Workspace, model: ApiModel | None = None, *, env: str | None = None
) -> FakeLayout:
    model = model or shop_model()
    fake = ws.fake(model.name)
    write_model(fake.model_path, model)
    now = datetime.now(UTC)
    progress = Progress(fake=model.name, source=model.source.url, started_at=now, updated_at=now)
    write_model(fake.progress_path, progress)
    fake.env_file.write_text(env if env is not None else f"FAUXCALL_PSEUDONYM_KEY={KEY}\n")
    return fake


def write_plan(fake: FakeLayout, name: str, calls: list[dict[str, Any]]) -> Path:
    path = fake.plans / f"{name}.json"
    plan = RecordingPlan.model_validate(
        {"name": name, "created_at": datetime.now(UTC), "calls": calls}
    )
    write_model(path, plan)
    return path


def get(call_id: str, path: str, endpoint_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": call_id,
        "endpoint": endpoint_id,
        "request": {"method": "GET", "path": path},
        **extra,
    }


def files_text(root: Path) -> str:
    return "\n".join(
        path.read_text(errors="replace") for path in sorted(root.rglob("*")) if path.is_file()
    )


def exchanges(fake: FakeLayout) -> list[Exchange]:
    return [exchange for _, exchange in RecordingStore(fake.recordings).iter_exchanges()]


def record_shop(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> FakeLayout:
    """Record the shop API: reads (twice), a 404, an approved create with read-back, a job."""
    serve_with(monkeypatch, real_api())
    fake = make_fake(ws)
    reads = write_plan(
        fake,
        "reads",
        [
            get("list", "/v1/items", "GET /v1/items", repeat=2),
            get(
                "one",
                "/v1/items/{id}",
                "GET /v1/items/{id}",
                request={"method": "GET", "path": "/v1/items/{id}", "path_params": {"id": "1"}},
            ),
            get("missing", "/v1/items/999", "GET /v1/items/{id}", purpose="invalid"),
            get("users", "/v1/users", "GET /v1/users"),
        ],
    )
    invoke("record", "shop", str(reads))
    writes = write_plan(
        fake,
        "writes",
        [
            {
                "id": "create",
                "endpoint": "POST /v1/items",
                "purpose": "write_readback",
                "request": {
                    "method": "POST",
                    "path": "/v1/items",
                    "json_body": {"name": "Thing", "owner_id": 11, "price": 3.5},
                },
                "capture": {"new_id": "$.id"},
            },
            get("readback", "/v1/items/{{new_id}}", "GET /v1/items/{id}", purpose="write_readback"),
            {
                "id": "job",
                "endpoint": "POST /v1/jobs",
                "request": {"method": "POST", "path": "/v1/jobs"},
                "capture": {"job": "$.id"},
                "poll": {
                    "request": {"method": "GET", "path": "/v1/jobs/{{job}}"},
                    "endpoint": "GET /v1/jobs/{id}",
                    "state_path": "$.state",
                    "terminal": ["done", "failed"],
                    "interval_ms": 50,
                    "timeout_s": 10,
                },
            },
        ],
    )
    invoke("plan", "approve", "shop", "--planned", "--reason", "sandbox")
    invoke("record", "shop", str(writes))
    return fake


# -- plan template --------------------------------------------------------------------------------


def test_plan_template_fills_parameters(ws: Workspace) -> None:
    make_fake(ws)
    call = json.loads(invoke("plan", "template", "shop", "GET /v1/items/{id}").output)
    assert call["endpoint"] == "GET /v1/items/{id}"
    assert call["request"]["path_params"] == {"id": "7"}
    assert "query" not in call["request"]
    listing = json.loads(
        invoke("plan", "template", "shop", "GET /v1/items", "--purpose", "edge").output
    )
    assert listing["purpose"] == "edge"
    assert listing["request"]["query"] == [["limit", "1"]]
    create = json.loads(invoke("plan", "template", "shop", "POST /v1/items").output)
    assert create["request"]["json_body"] == {}


def test_plan_template_errors(ws: Workspace) -> None:
    make_fake(ws)
    assert (
        "unknown endpoint 'GET /nope'"
        in invoke("plan", "template", "shop", "GET /nope", code=1).output
    )
    result = invoke("plan", "template", "shop", "GET /v1/items", "--purpose", "fun", code=1)
    assert result.output.startswith("error:")


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"default": 5}, "5"),
        ({"enum": ["a", "b"]}, "a"),
        ({"type": "number"}, "1.5"),
        ({"type": "boolean"}, "true"),
        ({"type": "string"}, "value"),
        (None, "value"),
    ],
)
def test_plan_examples(schema: Any, expected: str) -> None:
    assert _example(schema) == expected


# -- plan review / approve / check ----------------------------------------------------------------


def test_plan_review_and_approve(ws: Workspace) -> None:
    fake = make_fake(ws)
    assert invoke("plan", "review", "shop").output.strip() == "No mutating calls planned."
    write_plan(
        fake,
        "writes",
        [
            {
                "id": "create",
                "endpoint": "POST /v1/items",
                "request": {"method": "POST", "path": "/v1/items"},
                "repeat": 2,
                "cleanup": ["undo"],
                "note": "make two",
            },
            {
                "id": "undo",
                "endpoint": "POST /v1/jobs",
                "request": {"method": "POST", "path": "/v1/jobs"},
            },
            get("read", "/v1/items", "GET /v1/items"),
        ],
    )
    review = invoke("plan", "review", "shop").output
    assert "| writes.json | create | `POST /v1/items` | 2 | undo | no | make two |" in review
    assert "Totals per endpoint: POST /v1/items x2, POST /v1/jobs x1" in review
    approved = invoke("plan", "approve", "shop", "--planned", "--margin", "1.5").output
    assert "approved POST /v1/items (max 3)" in approved
    assert "approved POST /v1/jobs (max 1)" in approved
    assert "| yes |" in invoke("plan", "review", "shop").output
    invoke("plan", "approve", "shop", "--endpoint", "DELETE /v1/*", "--reason", "cleanup")
    approvals = read_model(fake.approvals_path, Approvals)
    assert [a.endpoint for a in approvals.approvals][-1] == "DELETE /v1/*"
    assert approvals.approvals[-1].max_calls is None
    assert "(max unlimited)" in invoke("plan", "approve", "shop", "--endpoint", "PUT /x").output
    assert "nothing to approve" in invoke("plan", "approve", "shop", code=1).output


def test_plan_approve_per_endpoint_budgets(ws: Workspace) -> None:
    fake = make_fake(ws)
    approved = invoke(
        "plan", "approve", "shop", "--endpoint", "*", "--per-endpoint", "--max-calls", "5"
    ).output
    assert "approved POST /v1/items (max 5)" in approved
    assert "approved POST /v1/jobs (max 5)" in approved
    assert "GET " not in approved  # reads never need approval
    approvals = read_model(fake.approvals_path, Approvals)
    assert {a.endpoint for a in approvals.approvals} == {"POST /v1/items", "POST /v1/jobs"}
    missing = invoke("plan", "approve", "shop", "--endpoint", "PATCH *", "--per-endpoint", code=1)
    assert "no mutating endpoint matches" in missing.output
    model = ApiModel(
        name="tools",
        title="Tools",
        kind="mcp",
        source=SourceInfo(
            kind="mcp", url=f"{BASE}/mcp", fetched_at=datetime(2026, 1, 5, tzinfo=UTC)
        ),
        real=RealTarget(base_url=BASE),
        mcp=McpModel(tools=[McpToolModel(definition={"name": "ask"})]),
    )
    make_fake(ws, model)
    tools = invoke("plan", "approve", "tools", "--endpoint", "MCP *", "--per-endpoint").output
    assert "approved MCP tools/call:ask (max unlimited)" in tools


def test_plan_check(ws: Workspace, tmp_path: Path) -> None:
    fake = make_fake(ws)
    plan = write_plan(
        fake,
        "mixed",
        [
            get("read", "/v1/items", "GET /v1/items"),
            {
                "id": "create",
                "endpoint": "POST /v1/items",
                "request": {"method": "POST", "path": "/v1/items"},
            },
            {
                "id": "tool",
                "endpoint": "MCP tools/call:ask",
                "request": {"method": "POST", "path": "/mcp"},
                "mutating": False,
            },
        ],
    )
    output = invoke("plan", "check", "shop", str(plan)).output
    assert "- read: GET /v1/items x1 — read-only" in output
    assert "- create: POST /v1/items x1 — NEEDS APPROVAL" in output
    invoke("plan", "approve", "shop", "--endpoint", "POST /v1/items")
    assert "— approved" in invoke("plan", "check", "shop", str(plan)).output
    unknown = write_plan(fake, "unknown", [get("x", "/v2/x", "GET /v2/x")])
    assert (
        "unknown endpoints: GET /v2/x"
        in invoke("plan", "check", "shop", str(unknown), code=1).output
    )
    broken = tmp_path / "broken.json"
    broken.write_text('{"name": "b"}')
    assert "invalid plan" in invoke("plan", "check", "shop", str(broken), code=1).output
    missing = invoke("plan", "check", "shop", str(tmp_path / "missing.json"), code=1)
    assert missing.output.startswith("error:")


# -- record ---------------------------------------------------------------------------------------


def test_record_reads_writes_and_jobs(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = record_shop(ws, monkeypatch)
    recorded = exchanges(fake)
    by_endpoint: dict[str, list[Exchange]] = {}
    for exchange in recorded:
        by_endpoint.setdefault(exchange.endpoint, []).append(exchange)
    assert len(by_endpoint["GET /v1/items"]) == 2
    statuses = [e.response.status for e in by_endpoint["GET /v1/items/{id}"] if e.response]
    assert statuses == [200, 404, 200]
    created = by_endpoint["POST /v1/items"][0]
    assert created.response is not None
    assert created.response.status == 201
    polls = by_endpoint["GET /v1/jobs/{id}"]
    assert [e.purpose for e in polls] == ["job_poll"] * 3
    assert all("poll:job" in e.tags for e in polls)
    approvals = read_model(fake.approvals_path, Approvals)
    assert sum(approvals.used.values()) == 2


def test_record_skips_unapproved_writes(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    serve_with(monkeypatch, real_api())
    fake = make_fake(ws)
    plan = write_plan(
        fake,
        "w",
        [
            {
                "id": "c",
                "endpoint": "POST /v1/items",
                "request": {"method": "POST", "path": "/v1/items", "json_body": {}},
            }
        ],
    )
    output = invoke("record", "shop", str(plan)).output
    assert "needs approval" in output
    assert exchanges(fake) == []


def test_record_keeps_every_credential_out_of_recordings(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_with(monkeypatch, real_api())
    users = json.dumps(
        [{"name": "alice", "api_key": USER_KEY, "bearer_token": USER_TOKEN, "password": PASSWORD}]
    )
    headers = json.dumps({"Authorization": f"Bearer {TOKEN}", "X-Api-Key": API_KEY})
    query = json.dumps({"api_key": QUERY_KEY})
    env = (
        f"FAUXCALL_PSEUDONYM_KEY={KEY}\n"
        f"FAUXCALL_REAL__HEADERS='{headers}'\n"
        f"FAUXCALL_REAL__QUERY='{query}'\n"
        f"FAUXCALL_AUTH__USERS='{users}'\n"
        f"TYPESAFE_API_KEY={JEV}\n"
    )
    fake = make_fake(ws, env=env)
    plan = write_plan(
        fake,
        "echo",
        [
            get(
                "echo",
                "/v1/echo",
                "GET /v1/echo",
                request={
                    "method": "GET",
                    "path": "/v1/echo",
                    "query": [["note", PASSWORD], ["other", f"Bearer {TOKEN}"]],
                },
            )
        ],
    )
    invoke("record", "shop", str(plan))
    [exchange] = exchanges(fake)
    assert exchange.response is not None
    body = exchange.response.body.parsed()
    assert isinstance(body, dict)
    sent = body["headers"]
    assert isinstance(sent, dict)
    assert "authorization" in sent
    recorded = files_text(fake.recordings)
    for secret in (
        TOKEN,
        f"Bearer {TOKEN}",
        API_KEY,
        QUERY_KEY,
        USER_KEY,
        USER_TOKEN,
        PASSWORD,
        JEV,
    ):
        assert secret not in recorded, secret


def test_record_errors(ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    serve_with(monkeypatch, real_api())
    fake = make_fake(ws, env="")
    plan = write_plan(fake, "reads", [get("list", "/v1/items", "GET /v1/items")])
    assert "FAUXCALL_PSEUDONYM_KEY is missing" in invoke("record", "shop", str(plan), code=1).output
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert invoke("record", "shop", str(broken), code=1).output.startswith("error:")


def mcp_server(tmp_path: Path, *, path: str = "/mcp") -> FastAPI:
    model = ApiModel(
        name="server",
        title="Demo",
        kind="mcp",
        source=SourceInfo(kind="mcp", url="http://mcp.test/mcp", fetched_at=datetime.now(UTC)),
        real=RealTarget(base_url="http://mcp.test"),
        routing=RoutingModel(trailing_slash="redirect"),
        mcp=McpModel(
            transport=McpTransportModel(path=path),
            initialize_result={
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo", "version": "1"},
            },
            tools=[
                McpToolModel(
                    definition={
                        "name": "ask",
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": True},
                    }
                ),
                McpToolModel(definition={"name": "delete_all", "inputSchema": {"type": "object"}}),
            ],
        ),
    )
    settings = FakeSettings(data_dir=tmp_path / "server", fast=True, log_level="WARNING")
    return create_app(settings, runtime=FakeRuntime(settings, model), configure_logs=False)


def test_record_mcp_plan(ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    serve_with(monkeypatch, mcp_server(tmp_path))
    model = ApiModel(
        name="demo",
        title="Demo",
        kind="mcp",
        source=SourceInfo(kind="mcp", url="http://mcp.test/mcp", fetched_at=datetime.now(UTC)),
        real=RealTarget(base_url="http://mcp.test"),
        mcp=McpModel(
            tools=[
                McpToolModel(definition={"name": "ask", "annotations": {"readOnlyHint": True}}),
                McpToolModel(definition={"name": "delete_all"}),
            ]
        ),
    )
    fake = make_fake(ws, model)
    plan = write_plan(
        fake,
        "tools",
        [
            {
                "id": "ask",
                "endpoint": "MCP tools/call:ask",
                "request": {
                    "method": "POST",
                    "path": "/mcp",
                    "json_body": {"name": "ask", "arguments": {"q": "hi"}},
                },
            },
            {
                "id": "wipe",
                "endpoint": "MCP tools/call:delete_all",
                "request": {
                    "method": "POST",
                    "path": "/mcp",
                    "json_body": {"name": "delete_all", "arguments": {}},
                },
            },
        ],
    )
    output = invoke("record", "demo", str(plan)).output
    assert "needs approval" in output
    endpoints = {exchange.endpoint for exchange in exchanges(fake)}
    assert endpoints == {"MCP initialize", "MCP tools/call:ask"}


def test_record_mcp_uses_the_endpoint_init_resolved(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    serve_with(monkeypatch, mcp_server(tmp_path, path="/mcp/"))  # "/mcp" only redirects here
    model = ApiModel(
        name="demo",
        title="Demo",
        kind="mcp",
        source=SourceInfo(
            kind="mcp",
            url="http://mcp.test/mcp",
            spec_url="http://mcp.test/mcp/",
            fetched_at=datetime.now(UTC),
        ),
        real=RealTarget(base_url="http://mcp.test"),
        mcp=McpModel(
            tools=[McpToolModel(definition={"name": "ask", "annotations": {"readOnlyHint": True}})]
        ),
    )
    fake = make_fake(ws, model)
    call = {
        "id": "ask",
        "endpoint": "MCP tools/call:ask",
        "request": {"method": "POST", "path": "/mcp/", "json_body": {"name": "ask"}},
    }
    invoke("record", "demo", str(write_plan(fake, "tools", [call])))
    endpoints = {exchange.endpoint for exchange in exchanges(fake)}
    assert endpoints == {"MCP initialize", "MCP tools/call:ask"}


# -- sql ------------------------------------------------------------------------------------------


def test_sql(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    record_shop(ws, monkeypatch)
    table = invoke(
        "sql",
        "shop",
        "SELECT endpoint, count(*) AS n FROM exchanges GROUP BY endpoint ORDER BY endpoint",
    ).output
    assert "| endpoint | n |" in table
    assert "| GET /v1/items | 2 |" in table
    assert "read-only" in invoke("sql", "shop", "DELETE FROM exchanges", code=1).output
    assert "OperationalError" in invoke("sql", "shop", "SELECT nope FROM exchanges", code=1).output


def test_recordings_lists_filters_and_shows(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    record_shop(ws, monkeypatch)
    listing = invoke("recordings", "shop").output
    assert " GET /v1/items -> 200 " in listing
    assert "application/json" in listing
    only_404 = invoke("recordings", "shop", "--status", "404").output.strip().splitlines()
    assert len(only_404) == 1
    assert "-> 404" in only_404[0]
    one_endpoint = invoke("recordings", "shop", "--endpoint", "GET /v1/items/*").output
    assert all("/v1/items/" in line for line in one_endpoint.strip().splitlines())
    assert invoke("recordings", "shop", "--grep", "no-such-text").output.strip() == (
        "no recordings match"
    )
    limited = invoke("recordings", "shop", "--limit", "1").output.splitlines()
    assert limited[0].startswith("… ")
    assert len(limited) == 2
    first_id = listing.split()[0]
    shown = invoke("recordings", "shop", "--show", first_id.lstrip("0")).output
    assert json.loads(shown)["id"] == first_id
    assert "no recording" in invoke("recordings", "shop", "--show", "999999", code=1).output


def test_recordings_summarize_streams_binaries_and_failures(ws: Workspace) -> None:
    fake = make_fake(ws, shop_model())
    store = RecordingStore(fake.recordings)
    request = CapturedRequest(method=HttpMethod.GET, url=f"{BASE}/v1/items", path="/v1/items")
    when = datetime(2026, 1, 5, tzinfo=UTC)
    long_text = "x" * 200
    for response, stream in (
        (None, []),
        (CapturedResponse(status=200, body=Body(kind="binary", size=42)), []),
        (CapturedResponse(status=200), [StreamEvent(t_ms=1, kind="sse", data='{"n":1}')]),
        (CapturedResponse(status=200, body=Body(kind="text", text=long_text)), []),
    ):
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint="GET /v1/items",
                started_at=when,
                started_ns=0,
                request=request,
                response=response,
                stream=stream,
            )
        )
    lines = invoke("recordings", "shop").output.strip().splitlines()
    assert lines[0].endswith("-> no response  (no response)")
    assert lines[1].endswith("<42 bytes>")
    assert lines[2].endswith('<1 stream events> {"n":1}')
    assert lines[3].endswith("x…")


# -- analyze --------------------------------------------------------------------------------------


def test_analyze_endpoint_reports_and_applies(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = record_shop(ws, monkeypatch)
    report = invoke("analyze", "endpoint", "shop", "GET /v1/items/{id}").output
    assert report.startswith("# GET /v1/items/{id} — 3 recordings")
    assert "| 200 | 2 | application/json |" in report
    assert "| 404 | 1 | application/json |" in report
    assert "## Fields (successful responses)" in report
    assert "## Documented vs observed (200)" in report
    assert "Latency: p50" in report
    model = read_model(fake.model_path, ApiModel)
    assert model.endpoint("GET /v1/items/{id}").latency is None
    applied = invoke("analyze", "endpoint", "shop", "GET /v1/items/{id}", "--apply").output
    assert "model updated" in applied
    model = read_model(fake.model_path, ApiModel)
    updated = model.endpoint("GET /v1/items/{id}")
    assert updated.latency is not None
    assert [v.status for v in updated.responses] == [200, 404]
    assert isinstance(updated.responses[0].observed_schema, dict)
    assert updated.responses[0].content_type == "application/json"
    listing = invoke("analyze", "endpoint", "shop", "GET /v1/items").output
    assert "## Timestamp behavior" in listing


def test_analyze_endpoint_applies_only_known_endpoints(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = record_shop(ws, monkeypatch)
    model = read_model(fake.model_path, ApiModel)
    model.endpoints = [e for e in model.endpoints if e.id != "GET /v1/users"]
    model.endpoints[0].collection = "missing"
    write_model(fake.model_path, model)
    output = invoke("analyze", "endpoint", "shop", "GET /v1/users", "--apply").output
    assert "model updated" not in output
    problems = invoke("analyze", "endpoint", "shop", "POST /v1/items", "--apply").output
    assert (
        "model updated (problems: endpoint 'GET /v1/items': unknown collection 'missing')"
        in problems
    )


def test_analyze_endpoint_without_recordings(ws: Workspace) -> None:
    make_fake(ws)
    assert (
        "no recordings for 'GET /v1/items'"
        in invoke("analyze", "endpoint", "shop", "GET /v1/items", code=1).output
    )


def test_analyze_relations(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = record_shop(ws, monkeypatch)
    table = invoke("analyze", "relations", "shop").output
    assert "| from | field | to | key field |" in table
    assert "GET /v1/users" in table
    model = read_model(fake.model_path, ApiModel)
    model.collections = [CollectionModel(name="items"), CollectionModel(name="users")]
    write_model(fake.model_path, model)
    save_collection(
        fake.data / "world/items.jsonl",
        [WorldRecord(key=i, record={"id": i, "owner_id": 9 + i}) for i in (1, 2)],
    )
    save_collection(
        fake.data / "world/users.jsonl", [WorldRecord(key=i, record={"id": i}) for i in (10, 11)]
    )
    world = invoke("analyze", "relations", "shop", "--world").output
    assert "| items | `$.owner_id` | users | `$.id` |" in world


def test_analyze_jobs(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    record_shop(ws, monkeypatch)
    output = invoke(
        "analyze",
        "jobs",
        "shop",
        "--endpoint",
        "GET /v1/jobs/{id}",
        "--state-path",
        "$.state",
        "--terminal",
        "done,failed",
    ).output
    assert output.startswith("- job: queued [")
    assert "→ done" in output
    summary = json.loads(output.split("\n\n", 1)[1])
    assert summary["jobs"] == 1
    assert summary["outcomes"] == [{"state": "done", "probability": 1.0}]


def test_analyze_writes(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = record_shop(ws, monkeypatch)
    recorded = exchanges(fake)
    write = next(e for e in recorded if e.endpoint == "POST /v1/items")
    readback = next(e for e in recorded if e.plan_call == "readback")
    output = invoke(
        "analyze", "writes", "shop", "--write", write.id, "--readback", readback.id
    ).output
    assert "response echoes the record" in output
    assert "read-back" in output
    evidence = json.loads(output.split("\n", 1)[1])
    assert evidence["persisted"] is True
    assert evidence["id_field"] == "id"
    alone = invoke("analyze", "writes", "shop", "--write", write.id).output
    assert "read-back" not in alone
    assert (
        "unknown exchange id"
        in invoke("analyze", "writes", "shop", "--write", "999999", code=1).output
    )
    unknown_readback = invoke(
        "analyze", "writes", "shop", "--write", write.id, "--readback", "999999", code=1
    )
    assert "unknown exchange id" in unknown_readback.output


def test_analyze_errors_lists_and_saves_templates(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = record_shop(ws, monkeypatch)
    missing = next(
        e for e in exchanges(fake) if e.response is not None and e.response.status == 404
    )
    store = RecordingStore(fake.recordings)
    text_error = Exchange(
        id=store.next_id(),
        endpoint="GET /v1/users",
        started_at=datetime.now(UTC),
        started_ns=0,
        request=CapturedRequest(method=HttpMethod.GET, url=f"{BASE}/v1/users", path="/v1/users"),
        response=CapturedResponse(
            status=429,
            headers=[("Retry-After", "5"), ("X-Other", "1"), ("content-type", "text/plain")],
            body=Body(kind="text", content_type="text/plain", text="slow | down"),
        ),
    )
    store.save(text_error)
    listing = invoke("analyze", "errors", "shop").output
    assert (
        f'| `GET /v1/items/{{id}}` | 404 | `{{"error":"not found","id":999}}` | {missing.id} |'
        in listing
    )
    assert "| `GET /v1/users` | 429 | `slow / down` |" in listing
    saved = invoke(
        "analyze", "errors", "shop", "--template", "not_found", "--from", missing.id
    ).output
    assert "template 'not_found' saved" in saved
    assert "{{placeholders}}" in saved
    invoke("analyze", "errors", "shop", "--template", "slow", "--from", text_error.id)
    invoke("analyze", "errors", "shop", "--template", "not_found", "--from", missing.id)
    model = read_model(fake.model_path, ApiModel)
    assert [t.name for t in model.errors] == ["slow", "not_found"]
    slow = model.error_template("slow")
    assert slow.body_text == "slow | down"
    assert slow.headers == {"Retry-After": "5"}
    assert model.error_template("not_found").body_json == {"error": "not found", "id": 999}
    result = invoke("analyze", "errors", "shop", "--template", "x", "--from", "999999", code=1)
    assert "no error exchange '999999'" in result.output


def test_analyze_style(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = record_shop(ws, monkeypatch)
    style = json.loads(invoke("analyze", "style", "shop").output)
    assert style["key_separator"] == ":"
    model = read_model(fake.model_path, ApiModel)
    assert model.json_style.key_separator == ":"


def test_analyze_endpoint_with_streams_and_text_bodies(ws: Workspace) -> None:
    fake = make_fake(ws)
    store = RecordingStore(fake.recordings)
    for offset in range(2):
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint="GET /v1/echo",
                started_at=datetime.now(UTC) - timedelta(seconds=offset),
                started_ns=offset,
                elapsed_ms=12.5,
                request=CapturedRequest(
                    method=HttpMethod.GET, url=f"{BASE}/v1/echo", path="/v1/echo"
                ),
                response=CapturedResponse(status=200, body=Body(kind="text", text="plain")),
            )
        )
    store.save(
        Exchange(
            id=store.next_id(),
            endpoint="GET /v1/echo",
            started_at=datetime.now(UTC),
            started_ns=5,
            request=CapturedRequest(method=HttpMethod.GET, url=f"{BASE}/v1/echo", path="/v1/echo"),
            error="ConnectError",
        )
    )
    output = invoke("analyze", "endpoint", "shop", "GET /v1/echo", "--apply").output
    assert "## Fields" not in output
    assert "Latency: p50 12 ms" in output
    assert "model updated" in output
    model = read_model(fake.model_path, ApiModel)
    assert model.json_style == shop_model().json_style
    assert [v.status for v in model.endpoint("GET /v1/echo").responses] == [200]
    assert "| from | field |" in invoke("analyze", "relations", "shop").output
