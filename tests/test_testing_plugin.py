from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from fauxcall.model.api import ApiModel, RealTarget, SourceInfo
from fauxcall.model.endpoints import EndpointModel, ResponseShape
from fauxcall.model.fields import IgnorePolicy
from fauxcall.testing.client import CompareOptions, ExchangeLog, LoggedCall
from fauxcall.testing.compare import Observation
from fauxcall.testing.plugin import (
    FakeUnderTest,
    ParitySession,
    compare_logs,
    parity_session,
    record_roots,
)
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from fastapi import FastAPI

    from fauxcall.model.endpoints import EndpointRole

pytest_plugins = ["pytester"]

INI = """
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
"""

REST_CONFTEST = """
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from fauxcall.model.api import ApiModel, RealTarget, SourceInfo
from fauxcall.model.endpoints import EndpointModel
from fauxcall.model.fields import IgnorePolicy
from fauxcall.testing import FakeUnderTest
from fauxcall.types import HttpMethod

HERE = Path(__file__).parent


def pets_app(color: str) -> FastAPI:
    app = FastAPI()

    @app.get("/pets/findByStatus")
    async def find(status: str = "available") -> Response:
        return JSONResponse([{"id": 1, "status": status, "color": color}])

    @app.get("/pets/{pet_id}")
    async def get_pet(pet_id: int) -> Response:
        return JSONResponse({"id": pet_id, "color": color})

    @app.post("/pets")
    async def create() -> Response:
        return JSONResponse({"id": 2}, status_code=201)

    @app.get("/whoami")
    async def whoami(request: Request) -> Response:
        return JSONResponse(
            {"key": request.headers.get("x-key"), "token": request.query_params.get("token")}
        )

    return app


MODEL = ApiModel(
    name="pets",
    title="Pets",
    source=SourceInfo(
        kind="openapi",
        url="https://pets.example.com/openapi.json",
        fetched_at=datetime(2026, 10, 10, tzinfo=UTC),
    ),
    real=RealTarget(base_url="https://pets.example.com"),
    endpoints=[
        EndpointModel(id="GET /pets/{pet_id}", method=HttpMethod.GET, path="/pets/{pet_id}"),
        EndpointModel(
            id="GET /pets/findByStatus",
            method=HttpMethod.GET,
            path="/pets/findByStatus",
            fields=[IgnorePolicy(path="$[*].color", scope="body")],
        ),
        EndpointModel(id="POST /pets", method=HttpMethod.POST, path="/pets"),
    ],
)


@pytest.fixture(scope="session")
def fauxcall_fake() -> FakeUnderTest:
    mode = os.environ.get("REAL_MODE", "transport")
    color = os.environ.get("FAKE_COLOR", "brown")
    return FakeUnderTest(
        app_factory=lambda: pets_app(color),
        model=MODEL,
        real_base_url="https://real.invalid" if mode == "url" else None,
        real_headers={"x-key": "k1"},
        real_query={"token": "t1"},
        reports_dir=None if os.environ.get("REPORTS") == "default" else HERE / "reports",
        real_transport=httpx2.ASGITransport(app=pets_app("brown")) if mode == "transport" else None,
    )
"""

SCENARIOS = """
import pytest


async def test_get_pet(api):
    response = await api.get("/pets/1")
    assert response.status_code == 200


async def test_find(api):
    response = await api.get("/pets/findByStatus", params={"status": "sold"})
    assert response.json()[0]["status"] == "sold"


@pytest.mark.parametrize("n", [1, 2])
async def test_param(api, n):
    assert (await api.get(f"/pets/{n}")).status_code == 200


async def test_create(api):
    response = await api.post("/pets", json={"name": "Rex"})
    assert response.status_code == 201


def test_plain():
    assert True
"""

CREDENTIALS = """
async def test_whoami(api):
    body = (await api.get("/whoami")).json()
    expected = {"key": "k1", "token": "t1"} if api.is_real else {"key": None, "token": None}
    assert body == expected


async def test_embedded_query_survives_query_credentials(api):
    pets = (await api.get("/pets/findByStatus?status=pending")).json()
    assert pets[0]["status"] == "pending"
"""

URLS = """
import os


async def test_base_url(api):
    assert str(api.http.base_url).rstrip("/") == os.environ["EXPECTED_BASE_URL"]
"""

PARTIAL = """
async def test_stops_early(api):
    await api.get("/pets/1")
    if not api.is_real:
        raise AssertionError("the fake broke")
    await api.get("/pets/2")
"""

MCP_CONFTEST = """
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi import APIRouter, FastAPI
from starlette.responses import JSONResponse, Response

from fauxcall.config import FakeSettings
from fauxcall.model.api import ApiModel, RealTarget, SourceInfo
from fauxcall.model.auth import AuthModel, AuthScheme
from fauxcall.model.mcp import McpModel, McpToolModel
from fauxcall.runtime import FakeRuntime, create_app, register_tool
from fauxcall.testing import FakeUnderTest

HERE = Path(__file__).parent
AUTH = AuthModel(schemes=[AuthScheme(kind="bearer")]) if os.environ.get("AUTH") else None
ADD = {
    "name": "add",
    "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}}},
    "annotations": {"readOnlyHint": True},
}
RESET = {"name": "reset", "inputSchema": {"type": "object", "properties": {}}}
MODEL = ApiModel(
    name="tools",
    title="Tools",
    kind="mcp",
    source=SourceInfo(
        kind="mcp", url="https://mcp.example.com/mcp", fetched_at=datetime(2026, 10, 10, tzinfo=UTC)
    ),
    real=RealTarget(base_url="https://mcp.example.com"),
    auth=AUTH,
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


def tools_app(offset: int) -> FastAPI:
    settings = FakeSettings(name="tools", data_dir=HERE / "data", fast=True, log_level="WARNING")
    router = APIRouter()

    @router.get("/ping")
    async def ping() -> Response:
        return JSONResponse({"pong": True})

    app = create_app(
        settings, routers=[router], runtime=FakeRuntime(settings, MODEL), configure_logs=False
    )

    async def add(ctx, arguments):
        return {"content": [{"type": "text", "text": str(arguments["a"] + offset)}]}

    async def reset(ctx, arguments):
        return {"content": [{"type": "text", "text": "done"}]}

    register_tool(app, "add", add)
    register_tool(app, "reset", reset)
    return app


@pytest.fixture(scope="session")
def fauxcall_fake() -> FakeUnderTest:
    offset = int(os.environ.get("FAKE_OFFSET", "0"))
    return FakeUnderTest(
        app_factory=lambda: tools_app(offset),
        model=None if os.environ.get("NO_MODEL") else MODEL,
        real_headers={"Authorization": "Bearer real-token"} if AUTH else {},
        reports_dir=HERE / "reports",
        real_transport=httpx2.ASGITransport(app=tools_app(0)),
    )
"""

MCP_SCENARIOS = """
async def test_add(mcp):
    result = await mcp.call_tool("add", {"a": 2})
    assert result["content"][0]["text"] in {"2", "3"}


async def test_tools(mcp):
    listing = await mcp.list_tools()
    assert [tool["name"] for tool in listing["tools"]] == ["add", "reset"]


async def test_reset(mcp):
    result = await mcp.call_tool("reset")
    assert result["content"][0]["text"] == "done"


async def test_both(api, mcp):
    assert (await api.get("/ping")).json() == {"pong": True}
    await mcp.list_tools()
"""


AUTH_CONFTEST = """
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fauxcall.config import FakeSettings
from fauxcall.model.api import ApiModel, RealTarget, SourceInfo
from fauxcall.model.auth import AuthModel, AuthScheme
from fauxcall.model.endpoints import EndpointModel
from fauxcall.runtime import FakeRuntime, create_app
from fauxcall.runtime.dynamic import dynamic_router
from fauxcall.testing import FakeUnderTest
from fauxcall.types import HttpMethod

HERE = Path(__file__).parent
MODEL = ApiModel(
    name="secure",
    title="Secure",
    source=SourceInfo(
        kind="openapi",
        url="https://secure.example.com/openapi.json",
        fetched_at=datetime(2026, 10, 10, tzinfo=UTC),
    ),
    real=RealTarget(base_url="https://secure.example.com"),
    auth=AuthModel(
        schemes=[
            AuthScheme(kind="api_key", location="header", name="X-Api-Key"),
            AuthScheme(kind="api_key", location="query", name="token"),
            AuthScheme(kind="bearer"),
        ]
    ),
    endpoints=[EndpointModel(id="POST /echo", method=HttpMethod.POST, path="/echo", role="echo")],
)


def secure_app():
    settings = FakeSettings(name="secure", data_dir=HERE / "data", fast=True, log_level="WARNING")
    runtime = FakeRuntime(settings, MODEL)
    return create_app(
        settings, routers=[dynamic_router(MODEL)], runtime=runtime, configure_logs=False
    )


@pytest.fixture(scope="session")
def fauxcall_fake():
    return FakeUnderTest(app_factory=secure_app, model=MODEL)
"""

AUTH_SCENARIOS = """
async def test_scenarios_pass_with_placeholder_credentials(api):
    response = await api.post("/echo?x=1", json={"name": "Rex"})
    assert response.status_code == 200
    assert response.json() == {"name": "Rex"}


async def test_scenarios_can_still_send_missing_credentials(api):
    response = await api.post(
        "/echo",
        json={"name": "Rex"},
        headers={"X-Api-Key": "", "Authorization": ""},
        params={"token": ""},
    )
    assert response.status_code == 401
"""


ECHO_ENDPOINTS = (
    'endpoints=[EndpointModel(id="POST /echo", method=HttpMethod.POST, path="/echo", role="echo")],'
)
NOTES_ENDPOINTS = """endpoints=[
        EndpointModel(
            id="POST /notes", method=HttpMethod.POST, path="/notes", role="create",
            collection="notes",
        ),
        EndpointModel(
            id="GET /notes", method=HttpMethod.GET, path="/notes", role="list",
            collection="notes",
        ),
    ],
    collections=[CollectionModel(name="notes")],"""
ENDPOINTS_IMPORT = "from fauxcall.model.endpoints import EndpointModel"
COLLECTION_IMPORT = "from fauxcall.model.world import CollectionModel"
STATE_CONFTEST = (
    AUTH_CONFTEST.replace(ECHO_ENDPOINTS, NOTES_ENDPOINTS)
    .replace(ENDPOINTS_IMPORT, f"{ENDPOINTS_IMPORT}\n{COLLECTION_IMPORT}")
    .replace("def secure_app():", "BUILDS = []\n\n\ndef secure_app():\n    BUILDS.append(1)")
)

STATE_SCENARIOS = """
async def test_a_writes(api):
    assert (await api.post("/notes", json={"text": "hi"})).status_code == 200
    assert len((await api.get("/notes")).json()) == 1


async def test_b_starts_from_the_recorded_state(api):
    assert (await api.get("/notes")).json() == []


def test_c_the_app_was_built_once():
    import conftest

    assert len(conftest.BUILDS) == 1
"""


def test_scenarios_share_one_app_but_never_its_state(pytester: pytest.Pytester) -> None:
    pytester.makeini(INI)
    pytester.makeconftest(STATE_CONFTEST)
    pytester.makepyfile(test_state=STATE_SCENARIOS)
    run(pytester).assert_outcomes(passed=3)


@pytest.fixture
def secured(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini(INI)
    pytester.makeconftest(AUTH_CONFTEST)
    pytester.makepyfile(test_secure=AUTH_SCENARIOS)
    return pytester


@pytest.fixture
def rest(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    for name in ("REAL_MODE", "FAKE_COLOR", "REPORTS", "EXPECTED_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    pytester.makeini(INI)
    pytester.makeconftest(REST_CONFTEST)
    pytester.makepyfile(
        test_scenarios=SCENARIOS, test_credentials=CREDENTIALS, test_urls=URLS, test_partial=PARTIAL
    )
    return pytester


@pytest.fixture
def tools(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    for name in ("FAKE_OFFSET", "NO_MODEL", "AUTH"):
        monkeypatch.delenv(name, raising=False)
    pytester.makeini(INI)
    pytester.makeconftest(MCP_CONFTEST)
    pytester.makepyfile(test_tools=MCP_SCENARIOS)
    return pytester


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_inprocess(
        "-p", "fauxcall.testing.plugin", "-p", "no:cacheprovider", *args
    )


def report_json(pytester: pytest.Pytester, directory: str = "reports") -> Any:
    return json.loads((pytester.path / directory / "parity.json").read_text())


# -- fake and real targets -------------------------------------------------------------------------


def test_fake_target_runs_scenarios_in_process(rest: pytest.Pytester) -> None:
    result = run(rest, "test_scenarios.py", "test_credentials.py")
    result.assert_outcomes(passed=8)
    assert not (rest.path / "reports").exists()
    assert "fauxcall parity" not in result.stdout.str()


def test_real_target_uses_credentials_and_refuses_writes(rest: pytest.Pytester) -> None:
    result = run(rest, "--target=real", "test_scenarios.py", "test_credentials.py", "-rs")
    result.assert_outcomes(passed=7, skipped=1)
    result.stdout.fnmatch_lines(["*writes to the real API need --allow-real-writes*"])
    allowed = run(rest, "--target=real", "--allow-real-writes", "test_scenarios.py")
    allowed.assert_outcomes(passed=6)


def test_real_and_fake_base_urls(rest: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REAL_MODE", "url")
    monkeypatch.setenv("EXPECTED_BASE_URL", "https://real.invalid")
    run(rest, "--target=real", "test_urls.py").assert_outcomes(passed=1)
    monkeypatch.setenv("EXPECTED_BASE_URL", "http://127.0.0.1:9")
    run(rest, "--fake-url=http://127.0.0.1:9", "test_urls.py").assert_outcomes(passed=1)
    monkeypatch.setenv("EXPECTED_BASE_URL", "http://fake.test")
    run(rest, "test_urls.py").assert_outcomes(passed=1)


def test_real_target_without_a_real_api_skips(
    rest: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_MODE", "none")
    result = run(rest, "--target=real", "test_scenarios.py", "-rs")
    result.assert_outcomes(passed=1, skipped=5)
    result.stdout.fnmatch_lines(["*no real API configured*"])


# -- parity ----------------------------------------------------------------------------------------


def test_parity_passes_and_writes_reports(rest: pytest.Pytester) -> None:
    result = run(rest, "--target=parity", "test_scenarios.py")
    result.assert_outcomes(passed=10, skipped=1)  # the real create is a write
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*fauxcall parity*", "PASS: compliance 100.00%*", "report: *"])
    data = report_json(rest)
    assert data["ok"] is True
    assert data["unmatched_scenarios"] == []
    assert set(data["scenarios"]) == {
        "test_scenarios.py::test_get_pet",
        "test_scenarios.py::test_find",
        "test_scenarios.py::test_param[1]",
        "test_scenarios.py::test_param[2]",
        "test_scenarios.py::test_create",
    }
    assert data["scenarios"]["test_scenarios.py::test_create"] == []  # nothing ran on real
    assert set(data["endpoints"]) == {"GET /pets/{pet_id}", "GET /pets/findByStatus"}
    assert (rest.path / "reports" / "parity.md").read_text().startswith("# Parity report — PASS")


def test_parity_fails_below_the_threshold_and_sessions_never_mix(
    rest: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COLOR", "black")
    failing = run(rest, "--target=parity", "test_scenarios.py")
    failing.assert_outcomes(passed=10, skipped=1)
    assert failing.ret == pytest.ExitCode.TESTS_FAILED
    failing.stdout.fnmatch_lines(["FAIL: compliance *"])
    data = report_json(rest)
    assert data["ok"] is False
    assert data["score"] < 0.98
    assert data["endpoints"]["GET /pets/findByStatus"]["score"] == 1.0  # color ignored there
    failures = data["scenarios"]["test_scenarios.py::test_get_pet"][0]["failures"]
    assert failures == [{"check": "value $.color", "weight": 1.0, "detail": "'brown' vs 'black'"}]
    lenient = run(rest, "--target=parity", "--parity-threshold=0.9", "test_scenarios.py")
    assert lenient.ret == 0
    assert report_json(rest)["ok"] is True
    monkeypatch.setenv("FAKE_COLOR", "brown")
    assert run(rest, "--target=parity", "test_scenarios.py").ret == 0  # no state left over


def test_parity_without_a_real_api_fails_the_gate(
    rest: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_MODE", "none")
    monkeypatch.setenv("REPORTS", "default")
    result = run(rest, "--target=parity", "test_scenarios.py", "-k", "get_pet or find")
    result.assert_outcomes(passed=2, skipped=2)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*2 scenarios ran on one side only*"])
    data = report_json(rest, "parity-reports")
    assert data["unmatched_scenarios"] == [
        "test_scenarios.py::test_get_pet",
        "test_scenarios.py::test_find",
    ]


def test_parity_when_the_fake_run_stops_early(rest: pytest.Pytester) -> None:
    result = run(rest, "--target=parity", "test_partial.py")
    result.assert_outcomes(passed=1, failed=1)
    calls = report_json(rest)["scenarios"]["test_partial.py::test_stops_early"]
    assert calls[1] == {
        "call": "GET /pets/2",
        "score": 0.0,
        "failures": [
            {
                "check": "call made",
                "weight": 5.0,
                "detail": (
                    "test_partial.py::test_stops_early: the fake run stopped before this call"
                ),
            }
        ],
    }


def test_parity_with_no_scenarios_writes_nothing(rest: pytest.Pytester) -> None:
    result = run(rest, "--target=parity", "test_scenarios.py", "-k", "plain")
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    assert not (rest.path / "reports").exists()


# -- auth ------------------------------------------------------------------------------------------


def test_fake_side_sends_placeholder_credentials(secured: pytest.Pytester) -> None:
    run(secured).assert_outcomes(passed=2)


def test_mcp_fake_side_sends_placeholder_credentials(
    tools: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH", "1")
    reads = ("test_tools.py::test_add", "test_tools.py::test_tools")
    run(tools, *reads).assert_outcomes(passed=2)
    run(tools, "--target=real", *reads).assert_outcomes(passed=2)


# -- MCP -------------------------------------------------------------------------------------------


def test_mcp_scenarios_on_the_fake(tools: pytest.Pytester) -> None:
    run(tools).assert_outcomes(passed=4)


def test_mcp_writes_need_permission_on_the_real_side(tools: pytest.Pytester) -> None:
    run(tools, "--target=real").assert_outcomes(passed=3, skipped=1)
    run(tools, "--target=real", "--allow-real-writes").assert_outcomes(passed=4)


def test_mcp_tools_count_as_writes_without_a_model(
    tools: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_MODEL", "1")
    run(tools, "--target=real", "-k", "add").assert_outcomes(skipped=1)


def test_mcp_parity_per_tool(tools: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(tools, "--target=parity", "--allow-real-writes")
    result.assert_outcomes(passed=8)
    data = report_json(tools)
    assert data["ok"] is True
    assert set(data["endpoints"]) == {
        "GET /ping",
        "MCP tools/call:add",
        "MCP tools/call:reset",
        "MCP tools/list",
    }
    both = data["scenarios"]["test_tools.py::test_both"]
    assert [call["call"] for call in both] == ["GET /ping", "MCP tools/list"]
    monkeypatch.setenv("FAKE_OFFSET", "1")
    failing = run(tools, "--target=parity", "--allow-real-writes", "-k", "add")
    assert failing.ret == pytest.ExitCode.TESTS_FAILED
    failures = report_json(tools)["scenarios"]["test_tools.py::test_add"][0]["failures"]
    assert failures == [{"check": "value $.content[0].text", "weight": 1.0, "detail": "'2' vs '3'"}]


# -- units -----------------------------------------------------------------------------------------


def _endpoint(role: EndpointRole, shape: ResponseShape | None = None) -> EndpointModel:
    return EndpointModel(
        id="GET /pets",
        method=HttpMethod.GET,
        path="/pets",
        role=role,
        collection="pets" if role in {"get", "list", "search", "create"} else None,
        shape=shape or ResponseShape(),
    )


def test_record_roots_follow_the_endpoint_shape() -> None:
    assert record_roots(_endpoint("get")) == ["$"]
    assert record_roots(_endpoint("create")) == ["$"]
    assert record_roots(_endpoint("list")) == ["$[*]"]
    assert record_roots(_endpoint("search", ResponseShape(kind="envelope"))) == ["$.data[*]"]
    envelope = ResponseShape(kind="envelope", items_path="$.result.items")
    assert record_roots(_endpoint("list", envelope)) == ["$.result.items[*]"]
    assert record_roots(_endpoint("replay")) is None


def _call(path: str, body: object, *, options: CompareOptions | None = None) -> LoggedCall:
    observation = Observation(200, {"content-type": "application/json"}, body, None, 0)  # type: ignore[arg-type]
    return LoggedCall(f"GET {path}", "GET", path, observation, options or CompareOptions())


def test_compare_logs_uses_endpoint_and_scenario_policies() -> None:
    model = ApiModel(
        name="pets",
        title="Pets",
        source=SourceInfo(
            kind="openapi",
            url="https://x/openapi.json",
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        real=RealTarget(base_url="https://x"),
        endpoints=[
            EndpointModel(
                id="GET /pets/{id}",
                method=HttpMethod.GET,
                path="/pets/{id}",
                role="get",
                collection="pets",
                fields=[IgnorePolicy(path="seen")],
            )
        ],
    )
    spec = FakeUnderTest(app_factory=_no_app, model=model)
    scenario_policy = CompareOptions(policies=[IgnorePolicy(path="$.n", scope="body")])
    real = ExchangeLog(
        [
            _call("/pets/1", {"id": 1, "seen": "a", "owner": {"seen": "x"}}),
            _call("/other", {"n": 1, "m": 1}, options=scenario_policy),
            _call("/pets/2", {"id": 2}, options=CompareOptions(values=False, lengths=False)),
        ]
    )
    fake = ExchangeLog(
        [
            _call("/pets/1", {"id": 1, "seen": "b", "owner": {"seen": "y"}}),
            _call("/other", {"n": 2, "m": 1}),
            _call("/pets/2", {"id": 3}),
        ]
    )
    first, second, third = compare_logs("s", real, fake, spec)
    assert first.label == "GET /pets/1"
    assert [c.name for c in first.failures()] == ["value $.owner.seen"]  # only the record's own
    assert second.score == 1.0
    assert third.score == 1.0  # values not compared
    without_model = compare_logs("s", real, fake, FakeUnderTest(app_factory=_no_app))
    assert [c.name for c in without_model[0].failures()] == ["value $.seen", "value $.owner.seen"]


def _no_app() -> FastAPI:
    raise AssertionError("not used")


def test_parity_session_lives_in_the_config_stash(pytester: pytest.Pytester) -> None:
    config = pytester.parseconfig()
    state = parity_session(config)
    assert isinstance(state, ParitySession)
    assert parity_session(config) is state
    assert parity_session(pytester.parseconfig()) is not state
