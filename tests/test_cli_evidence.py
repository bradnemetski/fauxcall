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

from fauxcall import jsonfmt
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


def test_plan_template_for_mcp(ws: Workspace) -> None:
    create: JsonValue = {
        "name": "create_price",
        "inputSchema": {
            "type": "object",
            "properties": {
                "currency": {"type": "string", "enum": ["usd", "eur"]},
                "unit_amount": {"type": "integer"},
                "nickname": {"type": ["null", "string"]},
                "recurring": {"type": "object"},
                "tags": {"type": "array", "examples": [["a"]]},
                "active": {"type": "boolean", "default": False},
            },
            "required": [
                "currency",
                "unit_amount",
                "nickname",
                "recurring",
                "tags",
                "active",
                "undescribed",
            ],
        },
    }
    search: JsonValue = {
        "name": "search",
        "inputSchema": {"properties": {"q": {"type": "string"}, "n": {"const": 3}}},
    }
    model = ApiModel(
        name="tools",
        title="Tools",
        kind="mcp",
        source=SourceInfo(
            kind="mcp", url=f"{BASE}/mcp", fetched_at=datetime(2026, 1, 5, tzinfo=UTC)
        ),
        real=RealTarget(base_url=BASE),
        mcp=McpModel(
            transport=McpTransportModel(path="/v1/mcp"),
            tools=[
                McpToolModel(definition=create),
                McpToolModel(definition=search),
                McpToolModel(definition={"name": "bare", "inputSchema": "odd"}),
            ],
        ),
    )
    make_fake(ws, model)

    def template(endpoint: str, *options: str) -> Any:
        return json.loads(invoke("plan", "template", "tools", endpoint, *options).output)

    call = template("MCP tools/call:create_price", "--purpose", "variation")
    assert call["id"] == "create-price-variation"
    assert call["purpose"] == "variation"
    assert call["request"] == {
        "method": "POST",
        "path": "/v1/mcp",
        "json_body": {
            "name": "create_price",
            "arguments": {
                "currency": "usd",
                "unit_amount": 1,
                "nickname": "value",
                "recurring": {},
                "tags": ["a"],
                "active": False,
                "undescribed": "value",
            },
        },
    }
    assert template("MCP tools/call:search")["request"]["json_body"]["arguments"] == {
        "q": "value",
        "n": 3,
    }  # nothing required: every property
    assert template("MCP tools/call:bare")["request"]["json_body"] == {
        "name": "bare",
        "arguments": {},
    }
    read = template("MCP resources/read:ui://widget/a.html")
    assert read["request"]["json_body"] == {"uri": "ui://widget/a.html"}
    prompt = template("MCP prompts/get:summarize")
    assert prompt["request"]["json_body"] == {"name": "summarize", "arguments": {}}
    assert template("MCP tools/list")["request"]["json_body"] == {}
    unknown = invoke("plan", "template", "tools", "MCP tools/call:nope", code=1)
    assert "unknown tool 'nope'" in unknown.output


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
    assert "| writes.json | create | `POST /v1/items` | 2 | undo | **new** | make two |" in review
    assert "| writes.json | undo | `POST /v1/jobs` | 1 | **nothing: it stays on the real" in review
    assert "Totals per endpoint: POST /v1/items x2, POST /v1/jobs x1" in review
    assert "2 write(s) are new or changed since the user approved" in review
    assert "Parity runs may also write: default 100 per endpoint" in review
    approved = invoke("plan", "approve", "shop", "--planned", "--max-calls", "5").output
    assert "approved the reviewed call `POST /v1/items` (" in approved
    assert "for recording (max 2)" in approved  # exactly its `repeat`
    assert "approved POST /v1/items for parity runs (max 5)" in approved
    assert "approved POST /v1/jobs for parity runs (max 5)" in approved
    review = invoke("plan", "review", "shop").output
    assert "| create | `POST /v1/items` | 2 | undo | yes |" in review
    assert "new or changed" not in review
    again = invoke("plan", "approve", "shop", "--planned").output  # nothing new to approve
    assert "reviewed call" not in again
    invoke("plan", "approve", "shop", "--endpoint", "DELETE /v1/*", "--reason", "cleanup")
    approvals = read_model(fake.approvals_path, Approvals)
    assert [a.endpoint for a in approvals.approvals][-1] == "DELETE /v1/*"
    assert approvals.approvals[-1].max_calls == 100  # a glob shares one default budget
    unlimited = invoke("plan", "approve", "shop", "--endpoint", "PUT /x", "--unlimited").output
    assert "(max unlimited)" in unlimited
    both = invoke(
        "plan", "approve", "shop", "--endpoint", "PUT /y", "--unlimited", "--max-calls", "2", code=1
    )
    assert "not both" in both.output
    assert "nothing to approve" in invoke("plan", "approve", "shop", code=1).output


def test_analyze_access(ws: Workspace) -> None:
    fake = make_fake(ws)
    assert "Nothing recorded yet" in invoke("analyze", "access", "shop").output
    store = RecordingStore(fake.recordings)

    def recorded(endpoint: str, path: str, status: int) -> None:
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint=endpoint,
                started_at=datetime(2026, 1, 5, tzinfo=UTC),
                started_ns=0,
                request=CapturedRequest(method=HttpMethod.GET, url=f"{BASE}{path}", path=path),
                response=CapturedResponse(status=status),
            )
        )

    recorded("GET /v1/items", "/v1/items", 200)
    assert (
        "refused nowhere (1 endpoints or tools called)"
        in invoke("analyze", "access", "shop").output
    )
    recorded("GET /v1/items/{id}", "/v1/items/1", 403)
    recorded("GET /v1/users/{id}", "/v1/users/1", 200)
    recorded("GET /v1/users/{id}", "/v1/users/2", 403)
    output = invoke("analyze", "access", "shop").output
    assert "| `GET /v1/items/{id}` | 1 | 1 | HTTP 403 | /v1/items/1 |" in output
    assert "1 other endpoints or tools were never refused." in output
    assert "Refused every time: GET /v1/items/{id}." in output
    assert "Refused for some arguments: GET /v1/users/{id}." in output
    assert "The fake will refuse the same." in output


def test_the_answers_file_is_the_approval_in_headless_runs(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_fake(ws)
    answers = fake.root / "answers.json"
    answers.write_text(json.dumps({"name": "shop", "mutations": {"approve": False}}))
    assert "No mutating calls planned." in invoke("plan", "review", "shop").output
    assert not fake.approvals_path.exists()
    answers.write_text(
        json.dumps(
            {
                "name": "shop",
                "credentials": "none_needed",
                "mutations": {
                    "approve": True,
                    "endpoints": ["POST /v1/*", "DELETE /nowhere"],
                    "max_calls_per_endpoint": 7,
                    "reason": "a sandbox",
                },
            }
        )
    )
    review = invoke("plan", "review", "shop").output
    assert "applied the write approval in answers.json: POST /v1/items, POST /v1/jobs" in review
    approvals = read_model(fake.approvals_path, Approvals).approvals
    assert [(a.endpoint, a.max_calls, a.approved_by, a.reason) for a in approvals] == [
        ("POST /v1/items", 7, "answers.json", "a sandbox"),
        ("POST /v1/jobs", 7, "answers.json", "a sandbox"),
    ]
    assert "applied" not in invoke("plan", "review", "shop").output  # once only
    monkeypatch.setenv("FAUXCALL_APPROVAL_BUDGET", "3")
    answers.write_text(
        json.dumps({"mutations": {"approve": True, "endpoints": ["PUT *", "DELETE *"]}})
    )
    plan = write_plan(fake, "w", [get("read", "/v1/items", "GET /v1/items")])
    assert (
        "applied the write approval in answers.json"
        not in invoke("plan", "check", "shop", str(plan)).output
    )  # the model has no PUT or DELETE
    answers.write_text("{broken")
    assert "is not a valid answers file" in invoke("plan", "review", "shop", code=1).output


def test_a_changed_write_needs_approving_again(ws: Workspace) -> None:
    fake = make_fake(ws)

    def create(name: str) -> dict[str, Any]:
        return {
            "id": "create",
            "endpoint": "POST /v1/items",
            "request": {"method": "POST", "path": "/v1/items", "json_body": {"name": name}},
        }

    write_plan(fake, "writes", [create("a")])
    invoke("plan", "approve", "shop", "--planned")
    edited = create("b")
    plan = write_plan(fake, "writes", [edited])
    assert "NEEDS APPROVAL (new or changed)" in invoke("plan", "check", "shop", str(plan)).output
    assert "| **new** |" in invoke("plan", "review", "shop").output
    invoke("plan", "approve", "shop", "--planned")
    assert "— approved" in invoke("plan", "check", "shop", str(plan)).output
    approvals = read_model(fake.approvals_path, Approvals)
    (key,) = {Approvals.budget_key(a) for a in approvals.approvals if a.scope == "call"} - {
        Approvals.budget_key(approvals.approvals[0])
    }
    approvals.used[key] = 1  # recorded once: its approval is spent
    write_model(fake.approvals_path, approvals)
    assert "| yes, all its calls made |" in invoke("plan", "review", "shop").output
    more = invoke("plan", "approve", "shop", "--call", "create").output
    assert "for recording: 1 more calls (2 in all; 1 already spent)" in more
    assert (
        "no plan entry with id 'nope'"
        in invoke("plan", "approve", "shop", "--call", "nope", code=1).output
    )


def test_review_shows_failed_cleanups_and_writes_declared_read_only(ws: Workspace) -> None:
    fake = make_fake(ws)
    write_plan(
        fake,
        "writes",
        [
            {
                "id": "create",
                "endpoint": "POST /v1/items",
                "request": {"method": "POST", "path": "/v1/items"},
                "cleanup": ["remove"],
            },
            {
                "id": "remove",
                "endpoint": "POST /v1/jobs",
                "request": {"method": "POST", "path": "/v1/jobs"},
            },
            {
                "id": "sneaky",
                "endpoint": "POST /v1/jobs",
                "request": {"method": "POST", "path": "/v1/jobs"},
                "mutating": False,
                "note": "only validates",
            },
        ],
    )
    store = RecordingStore(fake.recordings)
    failed = Exchange(
        id=store.next_id(),
        endpoint="POST /v1/jobs",
        plan_call="remove",
        started_at=datetime(2026, 1, 5, tzinfo=UTC),
        started_ns=0,
        request=CapturedRequest(method=HttpMethod.POST, url="https://x/v1/jobs", path="/v1/jobs"),
        response=CapturedResponse(status=409),
    )
    store.save(failed)
    replies: list[tuple[str, JsonValue]] = [("other", ["fine"]), ("rpc", {"error": {"code": 1}})]
    for plan_call, reply in replies:
        store.save(
            failed.model_copy(
                update={
                    "id": store.next_id(),
                    "plan_call": plan_call,
                    "response": CapturedResponse(
                        status=200, body=Body(kind="json", json_value=reply)
                    ),
                }
            )
        )
    review = invoke("plan", "review", "shop").output
    assert "| create | `POST /v1/items` | 1 | remove (**failed when recorded**) |" in review
    assert "Declared read-only by the plans" in review
    assert "- writes.json `sneaky`: `POST /v1/jobs` (only validates)" in review
    write_plan(fake, "writes", [])
    write_plan(
        fake,
        "quiet",
        [
            {
                "id": "sneaky",
                "endpoint": "POST /v1/jobs",
                "request": {"method": "POST", "path": "/v1/jobs"},
                "mutating": False,
            }
        ],
    )
    only_declared = invoke("plan", "review", "shop").output
    assert only_declared.startswith("No mutating calls planned.")
    assert "`sneaky`: `POST /v1/jobs` (no reason given)" in only_declared


def test_the_default_budget_is_a_setting(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    make_fake(ws)
    monkeypatch.setenv("FAUXCALL_APPROVAL_BUDGET", "7")
    approved = invoke("plan", "approve", "shop", "--endpoint", "POST /v1/items").output
    assert approved.strip() == "approved POST /v1/items (max 7)"
    explicit = invoke("plan", "approve", "shop", "--endpoint", "POST /v1/jobs", "--max-calls", "3")
    assert explicit.output.strip() == "approved POST /v1/jobs (max 3)"


def test_approving_again_adds_to_the_budget(ws: Workspace) -> None:
    fake = make_fake(ws)
    first = invoke("plan", "approve", "shop", "--endpoint", "POST /v1/items", "--max-calls", "5")
    assert first.output.strip() == "approved POST /v1/items (max 5)"
    approvals = read_model(fake.approvals_path, Approvals)
    approvals.used = {"endpoint:POST /v1/items:": 7}  # spent past the budget (a lost count)
    write_model(fake.approvals_path, approvals)
    more = invoke("plan", "approve", "shop", "--endpoint", "POST /v1/items", "--max-calls", "3")
    assert (
        more.output.strip() == "approved POST /v1/items: 3 more calls (10 in all; 7 already spent)"
    )
    approvals = read_model(fake.approvals_path, Approvals)
    approvals.used = {"endpoint:POST /v1/items:": 8}
    write_model(fake.approvals_path, approvals)
    again = invoke("plan", "approve", "shop", "--endpoint", "POST /v1/items", "--max-calls", "4")
    assert "4 more calls (14 in all; 8 already spent)" in again.output  # on top of the 10 allowed
    invoke("plan", "approve", "shop", "--endpoint", "POST /v1/jobs", "--unlimited")
    unlimited = invoke("plan", "approve", "shop", "--endpoint", "POST /v1/jobs", "--max-calls", "2")
    assert unlimited.output.strip() == "approved POST /v1/jobs: already unlimited"


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
    assert "approved MCP tools/call:ask (max 100)" in tools


def test_mcp_plans_need_approval_exactly_where_recording_does(ws: Workspace) -> None:
    read_only: JsonValue = {"name": "read_docs", "annotations": {"readOnlyHint": True}}
    model = ApiModel(
        name="tools",
        title="Tools",
        kind="mcp",
        source=SourceInfo(
            kind="mcp", url=f"{BASE}/mcp", fetched_at=datetime(2026, 1, 5, tzinfo=UTC)
        ),
        real=RealTarget(base_url=BASE),
        mcp=McpModel(
            tools=[McpToolModel(definition={"name": "ask"}), McpToolModel(definition=read_only)]
        ),
    )
    fake = make_fake(ws, model)
    mcp = {"request": {"method": "POST", "path": "/mcp"}}
    plan = write_plan(
        fake,
        "mcp",
        [
            {"id": "hello", "endpoint": "MCP initialize", **mcp},
            {"id": "list", "endpoint": "MCP tools/list", **mcp},
            {"id": "read", "endpoint": "MCP tools/call:read_docs", **mcp},
            {"id": "ask", "endpoint": "MCP tools/call:ask", **mcp},
            {"id": "forced", "endpoint": "MCP resources/read:x", "mutating": True, **mcp},
        ],
    )
    checked = invoke("plan", "check", "tools", str(plan)).output
    assert "- hello: MCP initialize x1 — read-only" in checked  # the handshake never writes
    assert "- list: MCP tools/list x1 — read-only" in checked
    assert "- read: MCP tools/call:read_docs x1 — read-only" in checked
    assert "- ask: MCP tools/call:ask x1 — NEEDS APPROVAL" in checked  # not marked read-only
    assert "- forced: MCP resources/read:x x1 — NEEDS APPROVAL" in checked
    review = invoke("plan", "review", "tools").output
    assert "`MCP tools/call:ask`" in review
    assert "MCP initialize" not in review
    invoke("plan", "approve", "tools", "--planned", "--reason", "reads public docs")
    approved = read_model(fake.approvals_path, Approvals).approvals
    assert sorted((a.scope, a.use, a.endpoint) for a in approved) == [
        ("call", "recording", "MCP resources/read:x"),
        ("call", "recording", "MCP tools/call:ask"),
        ("endpoint", "parity", "MCP resources/read:x"),
        ("endpoint", "parity", "MCP tools/call:ask"),
    ]


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
    probe = write_plan(
        fake, "routing", [{**get("nope", "/v1/nope", "GET /v1/nope"), "purpose": "edge"}]
    )
    checked = invoke("plan", "check", "shop", str(probe)).output
    assert "- nope: GET /v1/nope x1 — read-only (routing probe: not in the model)" in checked
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


def test_record_exits_with_an_error_when_calls_fail(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    def client(*, follow_redirects: bool = True) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(refused), follow_redirects=follow_redirects
        )

    monkeypatch.setattr(common, "http_client", client)
    fake = make_fake(ws)
    plan = write_plan(fake, "r", [get("list", "/v1/items", "GET /v1/items")])
    output = invoke("record", "shop", str(plan), code=1).output
    assert "**1 call(s) failed** and recorded nothing: list." in output
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


def test_recordings_delete(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = record_shop(ws, monkeypatch)
    before = [e.id for e in exchanges(fake)]
    assert "say what to delete" in invoke("recordings-delete", "shop", code=1).output
    one = invoke("recordings-delete", "shop", "--id", before[0].lstrip("0")).output
    assert one.strip() == f"deleted 1 recordings: {before[0]}"
    polls = invoke(
        "recordings-delete", "shop", "--endpoint", "GET /v1/jobs/*", "--purpose", "job_poll"
    ).output
    assert polls.startswith("deleted 3 recordings")
    left = exchanges(fake)
    assert len(left) == len(before) - 4
    assert all(e.purpose != "job_poll" for e in left)
    assert invoke("recordings-delete", "shop", "--id", "999999").output.strip() == (
        "deleted 0 recordings"
    )


def test_recordings_of_mcp_calls_and_as_jsonl(ws: Workspace) -> None:
    fake = make_fake(ws)
    store = RecordingStore(fake.recordings)

    def call(tool: str, arguments: JsonValue, reply: JsonValue, status: int = 200) -> None:
        sent: JsonValue = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint=f"MCP tools/call:{tool}",
                plan_call=tool,
                started_at=datetime(2026, 1, 5, tzinfo=UTC),
                started_ns=0,
                elapsed_ms=12.4,
                request=CapturedRequest(
                    method=HttpMethod.POST,
                    url=f"{BASE}/mcp",
                    path="/mcp",
                    body=Body(kind="json", json_value=sent),
                ),
                response=CapturedResponse(
                    status=status,
                    body=Body(kind="json", json_value=reply) if reply else Body(kind="empty"),
                ),
            )
        )

    text: JsonValue = {"result": {"content": [{"type": "text", "text": "two\n prices"}]}}
    call("list", {"limit": 2}, text)
    call("read", {}, {"result": {"isError": True, "content": [{"type": "text", "text": "No"}]}})
    call("write", {"x": 1}, {"error": {"code": -32602, "message": "bad x"}})
    call("denied", {}, None, status=403)
    call("listed", {}, {"result": {"tools": []}})
    rows = invoke("recordings", "shop").output.splitlines()
    assert rows == [
        '000001 list [sample] tools/call:list {"limit":2} -> 200 ok 12ms  two prices',
        "000002 read [sample] tools/call:read -> 200 isError 12ms  No",
        '000003 write [sample] tools/call:write {"x":1} -> 200 error -32602 12ms  bad x',
        "000004 denied [sample] tools/call:denied -> 403 no reply 12ms",
        '000005 listed [sample] tools/call:listed -> 200 ok 12ms  {"result":{"tools":[]}}',
    ]
    lines = invoke("recordings", "shop", "--jsonl", "--endpoint", "MCP tools/call:list").output
    (line,) = lines.splitlines()
    parsed = json.loads(line)
    assert parsed["request"]["params"] == {"name": "list", "arguments": {"limit": 2}}
    assert parsed["response"] == text
    assert (parsed["status"], parsed["plan_call"], parsed["error"]) == (200, "list", None)


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
    assert "; errors (1): median" in report  # the 404 is timed apart from the answers
    model = read_model(fake.model_path, ApiModel)
    assert model.endpoint("GET /v1/items/{id}").latency is None
    applied = invoke("analyze", "endpoint", "shop", "GET /v1/items/{id}", "--apply").output
    assert "model updated" in applied
    model = read_model(fake.model_path, ApiModel)
    updated = model.endpoint("GET /v1/items/{id}")
    assert updated.latency is not None
    assert (len(updated.latency.samples_ms), len(updated.latency.error_samples_ms)) == (2, 1)
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


def test_analyze_a_tool_times_answers_and_errors_apart(ws: Workspace) -> None:
    model = shop_model()
    model.mcp = McpModel(tools=[McpToolModel(definition={"name": "ask"})])
    fake = make_fake(ws, model)
    store = RecordingStore(fake.recordings)
    answer: JsonValue = {"content": [{"type": "text", "text": "an answer"}]}
    replies: list[tuple[float, JsonValue]] = [
        (15000.0, {"jsonrpc": "2.0", "id": 1, "result": answer}),
        (12000.0, {"jsonrpc": "2.0", "id": 1, "result": answer}),
        (250.0, {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": True}}),
        (200.0, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}}),
    ]
    for elapsed, message in replies:
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint="MCP tools/call:ask",
                started_at=datetime.now(UTC),
                started_ns=0,
                elapsed_ms=elapsed,
                request=CapturedRequest(method=HttpMethod.POST, url=f"{BASE}/mcp", path="/mcp"),
                response=CapturedResponse(status=200, body=Body(kind="json", json_value=message)),
                # The answer came as an SSE event (Streamable HTTP): framing, not a stream.
                stream=[StreamEvent(t_ms=elapsed, kind="sse", data=jsonfmt.dumps(message))],
            )
        )
    output = invoke("analyze", "endpoint", "shop", "MCP tools/call:ask", "--apply").output
    assert "Latency: p50 13500 ms" in output
    assert "; errors (2): median 225 ms" in output
    assert "model updated" in output
    stored = read_model(fake.model_path, ApiModel)
    assert stored.mcp is not None
    tool = stored.mcp.tool("ask")
    assert tool is not None
    assert tool.latency is not None
    assert (tool.latency.samples_ms, tool.latency.error_samples_ms) == (
        [12000.0, 15000.0],
        [200.0, 250.0],
    )


def test_analyze_a_tool_learns_how_it_refuses_bad_arguments(ws: Workspace) -> None:
    schema: JsonValue = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    model = shop_model()
    model.mcp = McpModel(
        tools=[
            McpToolModel(definition={"name": "count", "inputSchema": schema}),
            McpToolModel(definition={"name": "loose"}),
            McpToolModel(definition={"name": "broken", "inputSchema": {"type": 5}}),
            McpToolModel(definition={"name": "strict", "inputSchema": schema}),
        ]
    )
    fake = make_fake(ws, model)
    store = RecordingStore(fake.recordings)

    def recorded(
        tool: str, arguments: JsonValue, reply: JsonValue, *, answered: bool = True
    ) -> None:
        sent: JsonValue = {"method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint=f"MCP tools/call:{tool}",
                started_at=datetime.now(UTC),
                started_ns=0,
                request=CapturedRequest(
                    method=HttpMethod.POST,
                    url=f"{BASE}/mcp",
                    path="/mcp",
                    body=Body(kind="json", json_value=sent),
                ),
                response=CapturedResponse(status=200, body=Body(kind="json", json_value=reply))
                if answered
                else None,
            )
        )

    rpc_error: JsonValue = {"error": {"code": -32602, "message": "Invalid params"}}
    failed: JsonValue = {"result": {"isError": True, "content": []}}
    fine: JsonValue = {"result": {"content": []}}
    recorded("count", {"n": "x"}, rpc_error)
    recorded("count", {}, rpc_error)
    recorded("count", {"n": 1.5}, failed)
    recorded("count", {"n": 3}, failed)  # valid arguments the server rejects: not counted
    recorded("count", {"n": "y"}, fine, answered=False)
    recorded("count", {"n": "z"}, fine)  # one answered as if fine
    output = invoke("analyze", "endpoint", "shop", "MCP tools/call:count", "--apply").output
    assert "answered as jsonrpc_error (2 of 4 probes)" in output
    rows = invoke("recordings", "shop", "--endpoint", "MCP tools/call:count").output
    assert '{"n":"y"} -> no response' in rows
    stored = read_model(fake.model_path, ApiModel)
    assert stored.mcp is not None
    tool = stored.mcp.tool("count")
    assert tool is not None
    assert tool.invalid_args == "jsonrpc_error"
    recorded("loose", {"anything": 1}, fine)
    assert (
        "Arguments that break"
        not in invoke("analyze", "endpoint", "shop", "MCP tools/call:loose").output
    )  # no schema: nothing can break it
    recorded("broken", {"n": 1}, fine)
    assert (
        "Arguments that break"
        not in invoke("analyze", "endpoint", "shop", "MCP tools/call:broken").output
    )  # an unusable schema
    recorded("strict", {"n": 1}, fine)
    strict = invoke("analyze", "endpoint", "shop", "MCP tools/call:strict").output
    assert "Arguments that break" not in strict  # every call kept to the schema


@pytest.mark.parametrize(
    ("answers", "verdict", "varies"),
    [
        (["a", "b", "c", "d"], "2 of 2 repeated: it varies", True),
        (["a", "b", "c", "c", "e", "e"], "1 of 3 repeated: mostly the same", False),
        (["a", "a"], "0 of 1 repeated: it answers them the same", False),
    ],
)
def test_analyze_a_tool_learns_whether_it_varies(
    ws: Workspace, answers: list[str], verdict: str, varies: bool
) -> None:
    model = shop_model()
    model.mcp = McpModel(tools=[McpToolModel(definition={"name": "search"})])
    fake = make_fake(ws, model)
    store = RecordingStore(fake.recordings)
    calls = [(f"q{index // 2}", text) for index, text in enumerate(answers)]
    calls += [("q9", "failed"), ("q9", "failed")]  # failures never count
    for query, text in calls:
        sent: JsonValue = {
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"q": query}},
        }
        result: JsonValue = (
            {"isError": True, "content": []}
            if text == "failed"
            else {"content": [{"type": "text", "text": text}], "_meta": {"session": query + text}}
        )
        store.save(
            Exchange(
                id=store.next_id(),
                endpoint="MCP tools/call:search",
                started_at=datetime.now(UTC),
                started_ns=0,
                request=CapturedRequest(
                    method=HttpMethod.POST,
                    url=f"{BASE}/mcp",
                    path="/mcp",
                    body=Body(kind="json", json_value=sent),
                ),
                response=CapturedResponse(
                    status=200, body=Body(kind="json", json_value={"result": result})
                ),
            )
        )
    output = invoke("analyze", "endpoint", "shop", "MCP tools/call:search", "--apply").output
    assert f"Same arguments, different answers: {verdict}" in output
    stored = read_model(fake.model_path, ApiModel)
    assert stored.mcp is not None
    tool = stored.mcp.tool("search")
    assert tool is not None
    assert tool.varies is varies


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


def test_analyze_stream(ws: Workspace) -> None:
    from fauxcall.model.recording import StreamEvent  # noqa: PLC0415

    model = ApiModel(
        name="pusher",
        title="Pusher",
        kind="mcp",
        source=SourceInfo(kind="mcp", url="http://mcp.test/mcp", fetched_at=datetime.now(UTC)),
        real=RealTarget(base_url="http://mcp.test"),
        mcp=McpModel(),
    )
    fake = make_fake(ws, model)
    assert "Nothing recorded" in invoke("analyze", "stream", "pusher").output
    store = RecordingStore(fake.recordings)
    pushed = [
        '{"method":"notifications/message","params":{"level":"info"},"jsonrpc":"2.0"}',
        "",  # a priming event
        "not json",
        '{"method":"roots/list","jsonrpc":"2.0","id":0}',
        '{"method":"notifications/message","params":{"level":"debug"},"jsonrpc":"2.0"}',
        '{"jsonrpc":"2.0","result":{}}',
    ]
    store.save(
        Exchange(
            id=store.next_id(),
            endpoint="MCP GET:stream",
            started_at=datetime(2026, 1, 5, tzinfo=UTC),
            started_ns=0,
            request=CapturedRequest(method=HttpMethod.GET, url="http://mcp.test/mcp", path="/mcp"),
            response=CapturedResponse(status=200),
            stream=[
                StreamEvent(t_ms=float(i), kind="sse", event="message", data=data)
                for i, data in enumerate(pushed)
            ],
        )
    )
    shown = invoke("analyze", "stream", "pusher").output
    assert "| `notifications/message` | 2 | notification |" in shown
    assert "| `roots/list` | 1 | request (the client answers) |" in shown
    assert "Member order: method, params, jsonrpc" in shown
    assert "--apply" in shown
    invoke("analyze", "stream", "pusher", "--apply")
    learned = read_model(fake.model_path, ApiModel).mcp
    assert learned is not None
    assert learned.server_messages == ["notifications/message", "roots/list"]
    assert learned.transport.message_member_order == ["method", "params", "jsonrpc"]
    rest = make_fake(ws)
    assert "is not an MCP fake" in invoke("analyze", "stream", rest.name, code=1).output
