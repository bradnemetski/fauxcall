"""CLI: init, status, progress, endpoints, model, spec, env, docs, mcp, doctor."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest
from typer.testing import CliRunner

from fauxcall import __version__
from fauxcall.cli import common
from fauxcall.cli import main as cli_main
from fauxcall.cli.main import app
from fauxcall.cli.project import _release, skill_version
from fauxcall.config import FakeSettings
from fauxcall.model.api import ApiModel, RealTarget, SourceInfo
from fauxcall.model.auth import AuthModel, AuthScheme
from fauxcall.model.endpoints import EndpointModel
from fauxcall.model.mcp import McpModel, McpToolModel
from fauxcall.model.progress import Progress
from fauxcall.paths import Workspace
from fauxcall.runtime.app import FakeRuntime, create_app
from fauxcall.storage import read_model, write_model
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from typer.testing import Result

FIXTURES = Path(__file__).parent / "fixtures" / "specs"
PETSTORE = "https://petstore3.swagger.io/api/v3/openapi.json"
PETSTORE_NAME = "swagger-petstore-openapi-3-0"
DOCS_URL = "https://docs.example.com/api/widgets"
MCP_URL = "http://mcp.test/mcp"
KEY = "test-pseudonym-key-0123456789abcdef"
runner = CliRunner()


# -- helpers --------------------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """An empty workspace; the CLI runs from ``tmp_path``, records fast, sees no stray env."""
    for variable in list(os.environ):
        if variable.startswith("FAUXCALL_") or variable.endswith("TYPESAFE_API_KEY"):
            monkeypatch.delenv(variable)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAUXCALL_WORKSPACE", str(tmp_path / "fakes"))
    monkeypatch.setenv("FAUXCALL_RECORD_RPS", "10000")
    return Workspace(tmp_path / "fakes")


def invoke(*args: str, code: int = 0) -> Result:
    result = runner.invoke(app, list(args))
    assert result.exit_code == code, (result.output, result.exception)
    return result


def serve_with(monkeypatch: pytest.MonkeyPatch, transport: httpx2.AsyncBaseTransport) -> None:
    def client(*, follow_redirects: bool = True) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=transport, follow_redirects=follow_redirects)

    monkeypatch.setattr(common, "http_client", client)


def static_site(pages: dict[str, tuple[str, str]]) -> httpx2.MockTransport:
    """URL -> (content type, body); everything else is a 404."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url).split("?", 1)[0]
        if request.method == "GET" and url in pages:
            content_type, body = pages[url]
            return httpx2.Response(200, text=body, headers={"content-type": content_type})
        return httpx2.Response(404, text="not found", headers={"content-type": "text/plain"})

    return httpx2.MockTransport(handler)


def petstore_site() -> httpx2.MockTransport:
    spec = (FIXTURES / "petstore3-openapi.json").read_text()
    return static_site({PETSTORE: ("application/json", spec)})


def mcp_model(
    *, tools: list[str], description: str = "Look things up", auth: bool = False
) -> ApiModel:
    definitions = [
        McpToolModel(
            definition={
                "name": tool,
                "description": description,
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                "annotations": {"readOnlyHint": True},
            }
        )
        for tool in tools
    ]
    return ApiModel(
        name="server",
        title="Demo MCP",
        kind="mcp",
        source=SourceInfo(kind="mcp", url=MCP_URL, fetched_at=datetime.now(UTC)),
        real=RealTarget(base_url="http://mcp.test"),
        auth=AuthModel(schemes=[AuthScheme(kind="bearer", prefix="Bearer ")]) if auth else None,
        mcp=McpModel(
            initialize_result={
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo", "title": "Demo MCP", "version": "1.0"},
            },
            tools=definitions,
        ),
    )


def mcp_app(tmp_path: Path, model: ApiModel) -> FastAPI:
    settings = FakeSettings(data_dir=tmp_path / "mcp-server", fast=True, log_level="WARNING")
    return create_app(settings, runtime=FakeRuntime(settings, model), configure_logs=False)


def write_fake(
    ws: Workspace, name: str, model: ApiModel, *, env: str | None = None, endpoints: bool = True
) -> None:
    fake = ws.fake(name)
    write_model(fake.model_path, model.model_copy(update={"name": name}))
    now = datetime.now(UTC)
    progress = Progress(fake=name, source=model.source.url, started_at=now, updated_at=now)
    if endpoints:
        for endpoint in model.endpoints:
            progress.endpoint(endpoint.id)
    write_model(fake.progress_path, progress)
    if env is not None:
        fake.env_file.write_text(env)


def rest_model() -> ApiModel:
    return ApiModel(
        name="shop",
        title="Shop",
        source=SourceInfo(
            kind="openapi", url="https://shop.test/openapi.json", fetched_at=datetime.now(UTC)
        ),
        real=RealTarget(base_url="https://shop.test", base_path="/v1"),
        endpoints=[
            EndpointModel(
                id="GET /v1/items", method=HttpMethod.GET, path="/v1/items", summary="List"
            ),
            EndpointModel(id="GET /v1/items/{id}", method=HttpMethod.GET, path="/v1/items/{id}"),
        ],
    )


def files_text(root: Path) -> str:
    return "\n".join(
        path.read_text(errors="replace") for path in sorted(root.rglob("*")) if path.is_file()
    )


# -- version / entry points -----------------------------------------------------------------------


def test_version_flag(ws: Workspace) -> None:
    assert invoke("--version").output.strip() == __version__


def test_no_arguments_shows_help(ws: Workspace) -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output
    assert "init" in result.output


def test_python_dash_m_runs_the_cli() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "fauxcall", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == __version__


def test_console_script_entry_point(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["fauxcall", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        cli_main.main()
    assert exit_info.value.code == 0


def test_invalid_cli_settings_fail_cleanly(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAUXCALL_RECORD_RPS", "fast")
    result = invoke("status", code=1)
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_unexpected_errors_are_not_swallowed(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Workspace:
        raise KeyError("boom")

    monkeypatch.setattr("fauxcall.cli.project.workspace", broken)
    result = runner.invoke(app, ["status"])
    assert isinstance(result.exception, KeyError)


# -- init -----------------------------------------------------------------------------------------


def test_init_from_an_openapi_url(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    serve_with(monkeypatch, petstore_site())
    result = invoke("init", PETSTORE)
    assert f"Created fake `{PETSTORE_NAME}`" in result.output
    assert f"fauxcall status {PETSTORE_NAME}" in result.output
    fake = ws.fake(PETSTORE_NAME)
    model = read_model(fake.model_path, ApiModel)
    assert model.real.base_url == "https://petstore3.swagger.io"
    assert model.real.base_path == "/api/v3"
    assert model.find_endpoint("GET /api/v3/pet/{petId}") is not None
    assert (fake.source_dir / "openapi.json").exists()
    assert stat.S_IMODE(fake.env_file.stat().st_mode) == 0o600
    assert (ws.root / ".gitignore").exists()


def test_init_with_a_name_and_refusing_to_overwrite(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_with(monkeypatch, petstore_site())
    invoke("init", PETSTORE, "--name", "pets")
    assert ws.fake("pets").model_path.exists()
    result = invoke("init", PETSTORE, "--name", "pets", code=1)
    assert "already exists" in result.output


def test_init_reports_discovery_failures_cleanly(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_with(monkeypatch, static_site({}))
    result = invoke("init", "https://nothing.test/spec.json", code=1)
    assert result.output.startswith("error:")
    assert "Traceback" not in result.output


def test_init_free_form_docs(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    page = (FIXTURES / "docs-page.html").read_text()
    serve_with(monkeypatch, static_site({DOCS_URL: ("text/html", page)}))
    result = invoke("init", DOCS_URL, "--base-url", "https://api.example.com/v1")
    assert "fauxcall spec import" in result.output
    fake = ws.fake("widgets-api-reference")
    assert "Widgets API" in (fake.source_dir / "docs.md").read_text()


def test_init_mcp_server_by_discovery(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = mcp_app(tmp_path, mcp_model(tools=["ask"]))
    serve_with(monkeypatch, httpx2.ASGITransport(app=server))
    result = invoke("init", MCP_URL, "--name", "demo")
    assert "MCP server **Demo MCP**" in result.output
    model = read_model(ws.fake("demo").model_path, ApiModel)
    assert model.kind == "mcp"
    assert model.mcp is not None
    assert [tool.name for tool in model.mcp.tools] == ["ask"]


def test_init_mcp_with_a_token_from_the_environment(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "tok-0123456789-secret"
    server = mcp_app(tmp_path, mcp_model(tools=["ask"], auth=True))
    serve_with(monkeypatch, httpx2.ASGITransport(app=server))
    monkeypatch.setenv("DEMO_TOKEN", token)
    invoke("init", MCP_URL, "--mcp", "--name", "demo", "--auth-env", "DEMO_TOKEN")
    recordings = ws.fake("demo").recordings
    assert any(recordings.rglob("*.json"))
    assert token not in files_text(recordings)


def test_init_discovers_an_auth_protected_mcp_server_with_the_token(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "tok-discovery-0123456789"
    server = mcp_app(tmp_path, mcp_model(tools=["ask"], auth=True))
    serve_with(monkeypatch, httpx2.ASGITransport(app=server))
    monkeypatch.setenv("DEMO_TOKEN", token)
    result = invoke("init", MCP_URL, "--name", "demo", "--auth-env", "DEMO_TOKEN")
    assert "MCP server **Demo MCP**" in result.output
    model = read_model(ws.fake("demo").model_path, ApiModel)
    assert model.kind == "mcp"
    assert token not in files_text(ws.fake("demo").root / "data")


def test_init_uses_the_credentials_the_user_prepared_in_env(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "tok-prepared-0123456789"
    server = mcp_app(tmp_path, mcp_model(tools=["ask"], auth=True))
    serve_with(monkeypatch, httpx2.ASGITransport(app=server))
    env = ws.fake("demo").env_file
    env.parent.mkdir(parents=True)
    env.write_text(f'FAUXCALL_REAL__HEADERS=\'{{"Authorization": "Bearer {token}"}}\'\n')
    result = invoke("init", MCP_URL, "--name", "demo")  # no --auth-env: the .env is enough
    assert "MCP server **Demo MCP**" in result.output
    assert token in env.read_text()  # the user's file is kept
    assert token not in files_text(ws.fake("demo").root / "data")


def test_init_without_the_token_cannot_discover_an_auth_protected_mcp_server(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = mcp_app(tmp_path, mcp_model(tools=["ask"], auth=True))
    serve_with(monkeypatch, httpx2.ASGITransport(app=server))
    result = invoke("init", MCP_URL, "--name", "demo", code=1)
    assert "authorization required" in result.output


def test_init_paces_mcp_introspection_with_the_recording_settings(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = mcp_app(tmp_path, mcp_model(tools=["ask"]))
    serve_with(monkeypatch, httpx2.ASGITransport(app=server))
    monkeypatch.setenv("FAUXCALL_RECORD_MAX_CALLS", "1")
    result = invoke("init", MCP_URL, "--mcp", "--name", "demo", code=1)
    assert "recording budget exhausted" in result.output


def test_init_auth_env_must_be_set(ws: Workspace) -> None:
    result = invoke("init", MCP_URL, "--mcp", "--auth-env", "NOT_SET_ANYWHERE", code=1)
    assert "NOT_SET_ANYWHERE is not set" in result.output


# -- status ---------------------------------------------------------------------------------------


def test_status_without_fakes(ws: Workspace) -> None:
    assert "No fakes yet" in invoke("status").output


def test_status_lists_every_fake_and_details_one(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_with(monkeypatch, petstore_site())
    invoke("init", PETSTORE, "--name", "pets")
    write_fake(ws, "shop", rest_model())
    listing = invoke("status").output
    assert "| pets | https://petstore3.swagger.io/api/v3/openapi.json |" in listing
    assert "| shop |" in listing
    invoke(
        "progress", "gate", "pets", "--credentials", "provided", "--jev", "--mutations", "approved"
    )
    invoke("progress", "note", "pets", "asked about the sandbox")
    invoke("progress", "phase", "pets", "record", "blocked", "--note", "waiting for a key")
    detail = invoke("status", "pets").output
    assert detail.startswith("# pets — next: **record: endpoint 'PUT /api/v3/pet' -> recorded**")
    assert "| record | blocked | waiting for a key |" in detail
    assert "| discover | done |" in detail
    assert "| `GET /api/v3/pet/{petId}` | pending | pending |" in detail
    assert "Gate: credentials=provided, jev_key=True, mutations=approved" in detail
    assert "asked about the sandbox" in detail


def test_status_of_a_fresh_fake_without_endpoints(ws: Workspace) -> None:
    write_fake(ws, "empty", rest_model(), endpoints=False)
    detail = invoke("status", "empty").output
    assert detail.startswith("# empty — next: **discover: pending**")
    assert "| endpoint |" not in detail
    assert "Gate:" not in detail
    assert "Recent notes" not in detail


def test_commands_work_from_inside_a_fake(
    ws: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAUXCALL_WORKSPACE", "fakes")  # the usual relative setting
    (tmp_path / ".git").mkdir()
    write_fake(ws, "shop", rest_model())
    monkeypatch.chdir(ws.fake("shop").root / "data")
    assert invoke("status", "shop").output.startswith("# shop")
    monkeypatch.chdir(tmp_path)
    assert "| shop |" in invoke("status").output


def test_status_of_an_unknown_fake(ws: Workspace) -> None:
    write_fake(ws, "shop", rest_model())
    result = invoke("status", "nope", code=1)
    assert "no fake called 'nope'" in result.output
    assert "known: shop" in result.output


# -- progress -------------------------------------------------------------------------------------


def test_progress_phase_records_timestamps_and_notes(ws: Workspace) -> None:
    write_fake(ws, "shop", rest_model())
    invoke("progress", "phase", "shop", "record", "in_progress", "--note", "started")
    invoke("progress", "phase", "shop", "record", "in_progress")
    progress = read_model(ws.fake("shop").progress_path, Progress)
    started = progress.phases["record"].started_at
    assert started is not None
    assert progress.phases["record"].notes == ["started"]
    invoke("progress", "phase", "shop", "record", "done")
    progress = read_model(ws.fake("shop").progress_path, Progress)
    assert progress.phases["record"].status == "done"
    assert progress.phases["record"].started_at == started
    assert progress.phases["record"].finished_at is not None


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["phase", "shop", "recording", "done"], "unknown phase 'recording'"),
        (["phase", "shop", "record", "finished"], "unknown state 'finished'"),
        (["endpoint", "shop", "all", "recorded", "finished"], "unknown state 'finished'"),
        (["endpoint", "shop", "all", "recording", "done"], "unknown step 'recording'"),
        (["gate", "shop", "--credentials", "maybe"], "unknown credentials answer 'maybe'"),
        (["gate", "shop", "--mutations", "maybe"], "unknown mutations answer 'maybe'"),
    ],
)
def test_progress_rejects_unknown_values(ws: Workspace, arguments: list[str], message: str) -> None:
    write_fake(ws, "shop", rest_model())
    before = ws.fake("shop").progress_path.read_text()
    result = invoke("progress", *arguments, code=1)
    assert message in result.output
    assert ws.fake("shop").progress_path.read_text() == before


def test_progress_endpoint_steps(ws: Workspace) -> None:
    write_fake(ws, "shop", rest_model())
    invoke("progress", "endpoint", "shop", "GET /v1/items", "recorded", "done", "--note", "3 calls")
    invoke("progress", "endpoint", "shop", "all", "analyzed", "skipped")
    invoke("progress", "endpoint", "shop", "GET /v1/new", "recorded", "in_progress")
    progress = read_model(ws.fake("shop").progress_path, Progress)
    items = progress.endpoint("GET /v1/items")
    assert items.steps["recorded"] == "done"
    assert items.notes == ["3 calls"]
    assert all(entry.steps["analyzed"] == "skipped" for entry in progress.endpoints[:2])
    assert progress.endpoint("GET /v1/new").steps["recorded"] == "in_progress"


def test_progress_gate_and_note(ws: Workspace) -> None:
    write_fake(ws, "shop", rest_model())
    assert (
        invoke("progress", "gate", "shop", "--note", "all good").output.strip() == "gate recorded"
    )
    invoke("progress", "note", "shop", "first note")
    progress = read_model(ws.fake("shop").progress_path, Progress)
    assert progress.gate is not None
    assert progress.gate.credentials == "none_needed"
    assert progress.gate.notes == ["all good"]
    assert progress.phases["gate"].status == "done"
    assert progress.log[-1].endswith(" first note")


# -- endpoints / model ----------------------------------------------------------------------------


def test_endpoints_table_and_detail(ws: Workspace) -> None:
    write_fake(ws, "shop", rest_model())
    table = invoke("endpoints", "shop").output
    assert "| `GET /v1/items` | replay |  | 0 | List |" in table
    detail = invoke("endpoints", "shop", "--show", "GET /v1/items/{id}").output
    assert json.loads(detail.split("\nrecordings:")[0])["path"] == "/v1/items/{id}"
    assert "recordings: 0" in detail
    assert (
        "unknown endpoint 'GET /x'"
        in invoke("endpoints", "shop", "--show", "GET /x", code=1).output
    )


def test_endpoints_lists_mcp_tools(ws: Workspace) -> None:
    write_fake(ws, "demo", mcp_model(tools=["ask", "read"]))
    output = invoke("endpoints", "demo").output
    assert "| `ask` | match | 0 |" in output
    assert "| `read` | match | 0 |" in output


def test_model_check(ws: Workspace) -> None:
    write_fake(ws, "shop", rest_model())
    assert "model OK: 2 endpoints, 0 collections" in invoke("model", "check", "shop").output
    broken = rest_model()
    broken.endpoints[0].collection = "missing"
    write_model(ws.fake("shop").model_path, broken)
    result = invoke("model", "check", "shop", code=1)
    assert "unknown collection 'missing'" in result.output
    ws.fake("shop").model_path.write_text('{"name": "shop"}')
    assert "model.json is invalid" in invoke("model", "check", "shop", code=1).output


def test_model_schema_and_show(ws: Workspace) -> None:
    schema = json.loads(invoke("model", "schema").output)
    assert schema["title"] == "ApiModel"
    part = json.loads(invoke("model", "schema", "--part", "EndpointModel").output)
    assert part["title"] == "EndpointModel"
    assert "unknown part 'Nope'" in invoke("model", "schema", "--part", "Nope", code=1).output
    brief = invoke("model", "schema", "--brief").output
    assert brief.startswith("## ApiModel\nRoot of")
    assert "\n## EndpointModel\n" in brief
    assert "- name: str (required)" in brief
    assert '- kind: "rest" | "mcp" = "rest"' in brief
    assert "- endpoints: list[EndpointModel]" in brief
    assert "- auth: AuthModel | null = null" in brief
    assert "- default_headers: dict[str, str] — Contract headers on every response." in brief
    one = invoke("model", "schema", "--part", "HttpMethod", "--brief").output
    assert one.startswith("## HttpMethod")
    assert "one of: " in one
    assert "## ApiModel" not in one
    whole = json.loads(invoke("model", "schema", "--part", "ApiModel").output)
    assert whole == schema
    write_fake(ws, "shop", rest_model())
    assert json.loads(invoke("model", "show", "shop").output)["title"] == "Shop"


# -- spec import ----------------------------------------------------------------------------------


def _widgets_spec() -> str:
    return json.dumps(
        {
            "openapi": "3.1.0",
            "info": {"title": "Widgets", "version": "1"},
            "paths": {
                "/widgets": {
                    "get": {"summary": "List widgets", "responses": {"200": {"description": "ok"}}}
                },
                "/v1/health": {"get": {"responses": {"200": {"description": "ok"}}}},
            },
        }
    )


def test_spec_import_adds_rebased_endpoints_once(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = (FIXTURES / "docs-page.html").read_text()
    serve_with(monkeypatch, static_site({DOCS_URL: ("text/html", page)}))
    invoke("init", DOCS_URL, "--name", "widgets", "--base-url", "https://api.example.com/v1")
    fake = ws.fake("widgets")
    (fake.source_dir / "openapi.json").write_text(_widgets_spec())
    assert invoke("spec", "import", "widgets").output.strip() == "added 2 endpoints"
    model = read_model(fake.model_path, ApiModel)
    assert [e.id for e in model.endpoints] == ["GET /v1/widgets", "GET /v1/health"]
    assert model.source.spec_file == "source/openapi.json"
    progress = read_model(fake.progress_path, Progress)
    assert [e.endpoint for e in progress.endpoints] == ["GET /v1/widgets", "GET /v1/health"]
    assert invoke("spec", "import", "widgets").output.strip() == "added 0 endpoints"


def test_spec_import_from_another_file(ws: Workspace, tmp_path: Path) -> None:
    write_fake(ws, "shop", rest_model())
    elsewhere = tmp_path / "written.json"
    elsewhere.write_text(_widgets_spec())
    assert invoke("spec", "import", "shop", str(elsewhere)).output.strip() == "added 2 endpoints"
    model = read_model(ws.fake("shop").model_path, ApiModel)
    assert model.source.spec_file is None
    assert model.find_endpoint("GET /v1/widgets") is not None


def test_spec_import_of_a_websocket_only_the_prose_mentions(ws: Workspace) -> None:
    write_fake(ws, "shop", rest_model())
    fake = ws.fake("shop")
    feed = {
        "openapi": "3.1.0",
        "info": {"title": "Quote feed (from the API description)", "version": "1"},
        "paths": {
            "/v1/ws/quotes": {
                "get": {
                    "summary": "WebSocket quote feed",
                    "description": 'Send {"action": "subscribe", "symbols": [...]}.',
                    "parameters": [
                        {"name": "api_key", "in": "query", "schema": {"type": "string"}}
                    ],
                    "responses": {"101": {"description": "Switching Protocols"}},
                }
            }
        },
    }
    written = fake.source_dir / "ws-quotes.openapi.json"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(json.dumps(feed))
    before = len(read_model(fake.model_path, ApiModel).endpoints)
    output = invoke("spec", "import", "shop", str(written)).output
    assert output.strip() == "added 1 endpoint"
    model = read_model(fake.model_path, ApiModel)
    assert len(model.endpoints) == before + 1
    endpoint = model.find_endpoint("GET /v1/ws/quotes")
    assert endpoint is not None
    assert [(p.name, p.location) for p in endpoint.params] == [("api_key", "query")]


def test_spec_import_errors(ws: Workspace, tmp_path: Path) -> None:
    write_fake(ws, "shop", rest_model())
    assert "error:" in invoke("spec", "import", "shop", code=1).output
    not_openapi = tmp_path / "not.json"
    not_openapi.write_text('{"hello": "world"}')
    result = invoke("spec", "import", "shop", str(not_openapi), code=1)
    assert result.output.startswith("error:")


# -- env ------------------------------------------------------------------------------------------


def test_env_check_never_prints_values(ws: Workspace) -> None:
    secret = "sk-live-0123456789"
    env = (
        f'FAUXCALL_REAL__HEADERS=\'{{"Authorization": "Bearer {secret}"}}\'\n'
        f"FAUXCALL_PSEUDONYM_KEY={KEY}\nTYPESAFE_API_KEY=jev-secret-123\n"
    )
    write_fake(ws, "shop", rest_model(), env=env)
    output = invoke("env", "check", "shop").output
    assert "- real headers: Authorization (Bearer …)" in output
    assert "- pseudonym key: set" in output
    assert "- Jev key (TYPESAFE_API_KEY): set" in output
    assert "- real base URL: (model default)" in output
    assert secret not in output
    assert "jev-secret-123" not in output


@pytest.mark.parametrize(
    ("headers", "shown"),
    [
        ('{"Authorization": "sk_test_0123456789"}', "Authorization (no scheme: most servers"),
        ('{"Authorization": "hunter2 0123456789"}', "Authorization (unrecognized scheme)"),
        ('{"X-API-Key": "sk_test_0123456789", "X-Empty": ""}', "X-API-Key, X-Empty (empty)"),
    ],
)
def test_env_check_works_before_init_and_shows_only_shapes(
    ws: Workspace, headers: str, shown: str
) -> None:
    fake = ws.fake("stripe")
    fake.root.mkdir(parents=True)
    fake.env_file.write_text(f"FAUXCALL_REAL__HEADERS='{headers}'\n")
    output = invoke("env", "check", "stripe").output
    assert f"- real headers: {shown}" in output
    assert "0123456789" not in output
    assert "hunter2" not in output
    assert "error:" in invoke("env", "check", "nothing-here", code=1).output


def test_env_key_creates_adds_or_keeps(ws: Workspace) -> None:
    write_fake(ws, "shop", rest_model())
    fake = ws.fake("shop")
    assert invoke("env", "key", "shop").output.strip() == "created .env with a new key"
    assert stat.S_IMODE(fake.env_file.stat().st_mode) == 0o600
    assert invoke("env", "key", "shop").output.strip() == "key already present"
    fake.env_file.write_text("FAUXCALL_REAL__BASE_URL=https://shop.test\n")
    assert invoke("env", "key", "shop").output.strip() == "key added"
    assert "FAUXCALL_PSEUDONYM_KEY=" in fake.env_file.read_text()
    assert "- pseudonym key: set" in invoke("env", "check", "shop").output


# -- docs -----------------------------------------------------------------------------------------


def test_docs_fetch_prints_or_writes(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = (FIXTURES / "docs-page.html").read_text()
    serve_with(monkeypatch, static_site({DOCS_URL: ("text/html", page)}))
    output = invoke("docs", "fetch", DOCS_URL).output
    assert "Widgets API" in output
    assert "## Links" in output
    assert "- https://docs.example.com/api/errors" in output
    target = tmp_path / "out" / "docs.md"
    assert (
        invoke("docs", "fetch", DOCS_URL, "--out", str(target)).output.strip() == f"wrote {target}"
    )
    assert "## Links" in target.read_text()


def test_docs_fetch_failure_is_clean(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    serve_with(monkeypatch, static_site({}))
    result = invoke("docs", "fetch", "https://docs.example.com/missing", code=1)
    assert result.output.startswith("error:")


# -- mcp introspect -------------------------------------------------------------------------------


def test_mcp_introspect_keeps_modeled_behavior(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = mcp_app(tmp_path, mcp_model(tools=["ask", "read"]))
    serve_with(monkeypatch, httpx2.ASGITransport(app=server))
    known = mcp_model(tools=["ask"])
    assert known.mcp is not None
    known.mcp.tools[0].behavior = "computed"
    known.mcp.tools[0].computed = "answer"
    write_fake(ws, "demo", known, env=f"FAUXCALL_PSEUDONYM_KEY={KEY}\n")
    output = invoke("mcp", "introspect", "demo").output
    assert output.startswith("2 tools;")
    model = read_model(ws.fake("demo").model_path, ApiModel)
    assert model.mcp is not None
    behaviors = {tool.name: (tool.behavior, tool.computed) for tool in model.mcp.tools}
    assert behaviors == {"ask": ("computed", "answer"), "read": ("match", None)}
    progress = read_model(ws.fake("demo").progress_path, Progress)
    assert [e.endpoint for e in progress.endpoints] == [
        "MCP tools/call:ask",
        "MCP tools/call:read",
    ]


def test_mcp_introspect_uses_the_recording_budget(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = mcp_app(tmp_path, mcp_model(tools=["ask"]))
    serve_with(monkeypatch, httpx2.ASGITransport(app=server))
    monkeypatch.setenv("FAUXCALL_RECORD_MAX_CALLS", "1")
    write_fake(ws, "demo", mcp_model(tools=["ask"]), env=f"FAUXCALL_PSEUDONYM_KEY={KEY}\n")
    result = invoke("mcp", "introspect", "demo", code=1)
    assert "recording budget exhausted" in result.output


def test_mcp_introspect_needs_a_pseudonym_key(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_fake(ws, "demo", mcp_model(tools=["ask"]))
    result = invoke("mcp", "introspect", "demo", code=1)
    assert "FAUXCALL_PSEUDONYM_KEY is missing" in result.output


def test_mcp_introspect_keeps_every_credential_out_of_recordings(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "mcp-token-5551234567"
    user_key = "alice-api-key-24680"
    user_token = "alice-bearer-13579"
    password = "alice-password-97531"
    jev = "jev-key-1122334455"
    leaky = f"Tools for {user_key} {user_token} {password} {jev} {token}"
    server = mcp_app(tmp_path, mcp_model(tools=["ask"], description=leaky, auth=True))
    serve_with(monkeypatch, httpx2.ASGITransport(app=server))
    users = json.dumps(
        [{"name": "alice", "api_key": user_key, "bearer_token": user_token, "password": password}]
    )
    env = (
        f"FAUXCALL_PSEUDONYM_KEY={KEY}\n"
        f'FAUXCALL_REAL__HEADERS=\'{{"Authorization": "Bearer {token}"}}\'\n'
        f"FAUXCALL_AUTH__USERS='{users}'\n"
        f"TYPESAFE_API_KEY={jev}\n"
    )
    write_fake(ws, "demo", mcp_model(tools=["ask"], auth=True), env=env)
    invoke("mcp", "introspect", "demo")
    recorded = files_text(ws.fake("demo").recordings)
    assert "tools/list" in recorded
    for secret in (token, user_key, user_token, password, jev):
        assert secret not in recorded


# -- shared helpers -------------------------------------------------------------------------------


async def test_http_client_uses_the_cli_settings(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAUXCALL_REQUEST_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("FAUXCALL_USER_AGENT", "tester/1")
    async with common.http_client(follow_redirects=False) as client:
        assert client.headers["user-agent"] == "tester/1"
        assert client.timeout.read == 7
        assert client.follow_redirects is False


# -- secret values --------------------------------------------------------------------------------


def test_secret_values_collects_every_credential() -> None:
    settings = FakeSettings.model_validate(
        {
            "real": {
                "headers": {"Authorization": "Bearer abc123456", "X-Key": "plain-key-1"},
                "query": {"api_key": "query-key-1"},
            },
            "auth": {
                "users": [
                    {"name": "a", "api_key": "user-key-1", "password": "pass word 1"},
                    {"name": "b", "bearer_token": "user-token-1"},
                    {"name": "c"},
                ]
            },
            "typesafe_api_key": "jev-key-1",
        }
    )
    assert common.secret_values(settings) == [
        "Bearer abc123456",
        "abc123456",
        "plain-key-1",
        "query-key-1",
        "user-key-1",
        "pass word 1",
        "word 1",
        "user-token-1",
        "jev-key-1",
    ]


def test_secret_values_without_credentials() -> None:
    settings = FakeSettings.model_validate({"real": {"headers": {"X-Empty": ""}}})
    assert common.secret_values(settings) == []


# -- doctor ---------------------------------------------------------------------------------------


def test_doctor_healthy(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str) -> str | None:
        return f"/usr/bin/{name}"

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setenv("TYPESAFE_API_KEY", "jev-1234")
    monkeypatch.setenv("FAUXCALL_SOURCE", "/src/fauxcall")
    output = invoke("doctor").output
    assert f"✓ fauxcall: {__version__}" in output
    assert "✓ uv on PATH: /usr/bin/uv" in output
    assert "✓ Jev key (optional): set" in output
    assert "✓ docker (optional): available" in output
    assert "✓ FAUXCALL_SOURCE: /src/fauxcall" in output


def test_doctor_without_uv(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    missing: Callable[[str], str | None] = lambda _name: None  # noqa: E731
    monkeypatch.setattr(shutil, "which", missing)
    result = invoke("doctor", code=1)
    assert "✗ uv on PATH: install: https://docs.astral.sh/uv/" in result.output
    assert "docker (optional): not found" in result.output
    assert f"fauxcall=={__version__} from PyPI" in result.output


def _plugin_skill(root: Path, version: str) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    skill = root / "skills" / "fauxcall"
    skill.mkdir(parents=True)
    return skill


def test_doctor_checks_the_skill_matches_the_cli(ws: Workspace, tmp_path: Path) -> None:
    same = _plugin_skill(tmp_path / "same", __version__)
    assert (
        f"✓ skill matches the CLI: {__version__}"
        in invoke("doctor", "--skill-dir", str(same)).output
    )
    older = _plugin_skill(tmp_path / "older", "0.0.1")
    result = invoke("doctor", "--skill-dir", str(older), code=1)
    assert f"✗ skill matches the CLI: skill 0.0.1, CLI {__version__}" in result.output
    assert "claude plugin update fauxcall@fauxcall" in result.output
    newer = _plugin_skill(tmp_path / "newer", "99.0.0")
    assert "uv tool upgrade fauxcall" in invoke("doctor", "--skill-dir", str(newer), code=1).output
    copied = tmp_path / "agents" / "skills" / "fauxcall"
    copied.mkdir(parents=True)
    assert "unknown (a copied skill)" in invoke("doctor", "--skill-dir", str(copied)).output


def test_skill_version_of_odd_plugins(tmp_path: Path) -> None:
    broken = _plugin_skill(tmp_path / "broken", "1")
    (tmp_path / "broken" / ".claude-plugin" / "plugin.json").write_text("not json")
    assert skill_version(broken) is None
    unversioned = _plugin_skill(tmp_path / "unversioned", "1")
    (tmp_path / "unversioned" / ".claude-plugin" / "plugin.json").write_text("{}")
    assert skill_version(unversioned) is None
    assert _release("0.1.10") > _release("0.1.2")
    assert _release("1.2.3rc1") == (1, 2, 3)
    assert _release("dev") == ()


def test_module_entry_point_in_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["fauxcall", "--version"])
    with pytest.raises(SystemExit) as finished:
        runpy.run_module("fauxcall", run_name="__main__", alter_sys=True)
    assert finished.value.code in {0, None}
    assert capsys.readouterr().out.strip() == __version__
