"""``fauxcall.build.init``: every kind of input, secrets handling, and the checkpoints it writes."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest

from _mcp import live_server, mcp_app, mcp_model
from fauxcall.build import init as init_module
from fauxcall.build.init import (
    GITIGNORE_REQUIRED,
    McpAccess,
    default_gitignore,
    ensure_env,
    ensure_workspace_gitignore,
    env_value,
    header_secrets,
    init_fake,
    origin,
    target_credentials,
)
from fauxcall.errors import ConfigurationError
from fauxcall.model import (
    ApiModel,
    AuthModel,
    AuthScheme,
    McpToolModel,
    McpTransportModel,
    RealTarget,
)
from fauxcall.model.api import RoutingModel
from fauxcall.model.progress import Progress
from fauxcall.paths import Workspace
from fauxcall.spec import DiscoveredSource
from fauxcall.storage import read_model

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES = Path(__file__).parent / "fixtures" / "specs"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
PETSTORE_URL = "https://petstore3.swagger.io/api/v3/openapi.json"
PETSTORE = (FIXTURES / "petstore3-openapi.json").read_text()
PETSTORE_NAME = "swagger-petstore-openapi-3-0"
DOCS_URL = "https://docs.example.com/api/widgets"
MCP_URL = "https://mcp.deepwiki.test/mcp"


def site(pages: dict[str, tuple[str, str]]) -> httpx2.MockTransport:
    """URL -> (content type, body) for GETs; everything else is a 404."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url).split("?", 1)[0]
        if request.method == "GET" and url in pages:
            content_type, body = pages[url]
            return httpx2.Response(200, text=body, headers={"content-type": content_type})
        return httpx2.Response(404, text="not found", headers={"content-type": "text/plain"})

    return httpx2.MockTransport(handler)


def client(transport: httpx2.AsyncBaseTransport) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=transport, follow_redirects=True)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "fakes")


async def init_petstore(ws: Workspace, **options: object) -> init_module.InitResult:
    async with client(site({PETSTORE_URL: ("application/json", PETSTORE)})) as http:
        return await init_fake(PETSTORE_URL, workspace=ws, http=http, now=NOW, **options)  # type: ignore[arg-type]


def everything_under(root: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in sorted(root.rglob("*")) if p.is_file())


# -- OpenAPI --------------------------------------------------------------------------------------


async def test_openapi_url_with_a_relative_server(ws: Workspace) -> None:
    result = await init_petstore(ws)
    model, layout = result.model, result.layout
    assert model.name == layout.name == PETSTORE_NAME
    assert model.title == "Swagger Petstore - OpenAPI 3.0"
    assert model.real == RealTarget(base_url="https://petstore3.swagger.io", base_path="/api/v3")
    assert model.find_endpoint("GET /api/v3/pet/{petId}") is not None
    assert model.find_endpoint("GET /api/v3/pet/findByStatus") is not None
    assert model.routing == RoutingModel(spec_paths=["/api/v3/openapi.json"])
    assert model.source.kind == "openapi"
    assert model.source.url == model.source.spec_url == PETSTORE_URL
    assert model.source.spec_file == "source/openapi.json"
    assert model.source.spec_version == "3.0.4"
    assert model.auth is not None
    assert (layout.source_dir / "openapi.json").read_text() == PETSTORE
    assert read_model(layout.model_path, ApiModel) == model
    progress = read_model(layout.progress_path, Progress)
    assert progress.phases["discover"].status == "done"
    assert progress.started_at == NOW
    assert [entry.endpoint for entry in progress.endpoints] == [e.id for e in model.endpoints]
    assert json.loads((layout.data / "privacy.json").read_text()) == {}
    assert "## Endpoints (19)" in result.summary
    assert "Suggested roles" in result.summary
    assert "suggested role **get** (collection `pet`)" in result.summary
    assert any("openapi" in note.lower() or "GET" in note for note in result.notes)


async def test_secrets_file_and_gitignore(ws: Workspace) -> None:
    result = await init_petstore(ws)
    env = result.layout.env_file
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "FAUXCALL_REAL__BASE_URL=https://petstore3.swagger.io\n" in env.read_text()
    key = env_value(result.layout, "FAUXCALL_PSEUDONYM_KEY")
    assert key is not None
    assert len(key) >= 32
    assert (ws.root / ".gitignore").read_text() == default_gitignore()
    for line in GITIGNORE_REQUIRED:
        assert line in default_gitignore().splitlines()


async def test_gitignore_exists_before_any_secret_is_written(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Path] = []
    original = init_module._create_private

    def checked(path: Path, text: str) -> bool:
        assert (ws.root / ".gitignore").exists(), "a secret was written before .gitignore"
        created.append(path)
        return original(path, text)

    monkeypatch.setattr(init_module, "_create_private", checked)
    result = await init_petstore(ws)
    assert created == [result.layout.env_file]


async def test_swagger_ui_page(ws: Workspace) -> None:
    pages = {
        "https://petstore3.swagger.io/": (
            "text/html",
            (FIXTURES / "petstore3-index.html").read_text(),
        ),
        "https://petstore3.swagger.io/swagger-initializer.js": (
            "application/javascript",
            (FIXTURES / "petstore3-swagger-initializer.js").read_text(),
        ),
        PETSTORE_URL: ("application/json", PETSTORE),
    }
    async with client(site(pages)) as http:
        result = await init_fake("https://petstore3.swagger.io/", workspace=ws, http=http)
    assert result.model.source.kind == "swagger_ui"
    assert result.model.source.url == "https://petstore3.swagger.io/"
    assert result.model.source.spec_url == PETSTORE_URL
    assert result.model.real.base_path == "/api/v3"
    assert result.model.routing.spec_paths == ["/api/v3/openapi.json"]


async def test_local_file_with_a_relative_server_asks_for_the_base_url(ws: Workspace) -> None:
    async with client(site({})) as http:
        with pytest.raises(ConfigurationError, match="--base-url"):
            await init_fake(str(FIXTURES / "petstore3-openapi.json"), workspace=ws, http=http)
    assert not ws.fake(PETSTORE_NAME).exists()


async def test_local_file_with_a_base_url_keeps_the_specs_base_path(ws: Workspace) -> None:
    spec = FIXTURES / "petstore3-openapi.json"
    async with client(site({})) as http:
        result = await init_fake(
            str(spec), workspace=ws, http=http, base_url="https://petstore.example.com"
        )
    model = result.model
    assert model.real == RealTarget(base_url="https://petstore.example.com", base_path="/api/v3")
    assert model.find_endpoint("GET /api/v3/pet/{petId}") is not None
    assert model.routing.spec_paths == []
    assert model.source.url == str(spec)
    assert model.source.spec_url == spec.resolve().as_uri()
    env = result.layout.env_file.read_text()
    assert "FAUXCALL_REAL__BASE_URL=https://petstore.example.com\n" in env


async def test_a_base_url_path_rebases_every_endpoint_and_auth_rule(ws: Workspace) -> None:
    result = await init_petstore(ws, base_url="https://staging.example.com/petstore/v3/")
    model = result.model
    assert model.real == RealTarget(
        base_url="https://staging.example.com", base_path="/petstore/v3"
    )
    assert model.find_endpoint("GET /petstore/v3/pet/{petId}") is not None
    assert all(e.path.startswith("/petstore/v3/") for e in model.endpoints)
    assert model.auth is not None
    rules = [*model.auth.required_for, *model.auth.exempt]
    assert rules
    assert all(" /petstore/v3/" in rule for rule in rules if rule != "*")
    assert model.routing.spec_paths == []  # the spec was served by another host


async def test_a_base_url_replaces_a_swagger2_host(ws: Workspace) -> None:
    async with client(site({})) as http:
        result = await init_fake(
            str(FIXTURES / "httpbin-spec.json"),
            workspace=ws,
            http=http,
            base_url="http://localhost:8080/api",
        )
    model = result.model
    assert model.real == RealTarget(base_url="http://localhost:8080", base_path="/api")
    assert model.find_endpoint("GET /api/get") is not None


async def test_a_base_url_must_be_absolute(ws: Workspace) -> None:
    async with client(site({})) as http:
        with pytest.raises(ConfigurationError, match="absolute URL"):
            await init_fake(
                str(FIXTURES / "petstore3-openapi.json"),
                workspace=ws,
                http=http,
                base_url="petstore.example.com",
            )


async def test_yaml_specs_are_snapshotted_as_yaml(ws: Workspace) -> None:
    spec = FIXTURES / "refs-openapi.yaml"
    async with client(site({})) as http:
        result = await init_fake(str(spec), workspace=ws, http=http, name="refs")
    assert result.model.source.spec_file == "source/openapi.yaml"
    assert (result.layout.source_dir / "openapi.yaml").read_text() == spec.read_text()
    assert result.model.real.base_url.startswith("https://")


async def test_a_non_http_server_is_refused(ws: Workspace, tmp_path: Path) -> None:
    spec = tmp_path / "ftp.json"
    spec.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "FTP", "version": "1"},
                "servers": [{"url": "ftp://files.example.com/x"}],
                "paths": {},
            }
        )
    )
    async with client(site({})) as http:
        with pytest.raises(ConfigurationError, match="not http"):
            await init_fake(str(spec), workspace=ws, http=http)


def test_the_discovered_spec_must_be_an_object() -> None:
    discovered = DiscoveredSource(kind="openapi", url="x.json", spec=[1, 2])
    with pytest.raises(ConfigurationError, match="not a JSON object"):
        init_module._from_openapi(discovered, "x.json", None, None, NOW)


async def test_reinit_of_an_existing_fake_fails_clearly(ws: Workspace) -> None:
    await init_petstore(ws)
    with pytest.raises(ConfigurationError, match=r"already exists.*fauxcall status"):
        await init_petstore(ws)


async def test_an_invalid_name_is_refused_before_anything_is_written(ws: Workspace) -> None:
    with pytest.raises(ConfigurationError, match="invalid fake name"):
        await init_petstore(ws, name="Not_A_Slug")
    assert not ws.root.exists()


# -- free-form documentation ----------------------------------------------------------------------


async def test_docs_page(ws: Workspace) -> None:
    page = (FIXTURES / "docs-page.html").read_text()
    async with client(site({DOCS_URL: ("text/html", page)})) as http:
        result = await init_fake(DOCS_URL, workspace=ws, http=http, now=NOW)
    model, layout = result.model, result.layout
    assert layout.name == "widgets-api-reference"
    assert model.source.kind == "html_docs"
    assert model.source.spec_file == "source/openapi.json"
    assert model.endpoints == []
    assert model.real == RealTarget(base_url="https://docs.example.com")
    docs = (layout.source_dir / "docs.md").read_text()
    assert "Widgets API" in docs
    assert "Copy" not in docs.splitlines()[0]
    links = json.loads((layout.source_dir / "docs-links.json").read_text())
    assert "https://docs.example.com/api/errors" in links
    assert read_model(layout.progress_path, Progress).endpoints == []
    assert f"fauxcall spec import {layout.name}" in result.summary
    assert "FAUXCALL_PSEUDONYM_KEY=" in layout.env_file.read_text()


async def test_docs_page_with_a_base_url(ws: Workspace) -> None:
    page = (FIXTURES / "docs-page.html").read_text()
    async with client(site({DOCS_URL: ("text/html", page)})) as http:
        result = await init_fake(
            DOCS_URL,
            workspace=ws,
            http=http,
            name="widgets",
            base_url="https://api.example.com/v1/",
        )
    assert result.model.real == RealTarget(base_url="https://api.example.com", base_path="/v1")
    assert "FAUXCALL_REAL__BASE_URL=https://api.example.com\n" in result.layout.env_file.read_text()


# -- MCP ------------------------------------------------------------------------------------------


async def test_mcp_server_by_discovery(ws: Workspace, tmp_path: Path) -> None:
    app, _ = mcp_app(tmp_path / "server", mcp_model())
    async with client(httpx2.ASGITransport(app=app)) as http:
        result = await init_fake(MCP_URL, workspace=ws, http=http, rps=10_000, now=NOW)
    model, layout = result.model, result.layout
    assert layout.name == "deepwiki-test"
    assert model.kind == "mcp"
    assert model.title == "DeepWiki"
    assert model.source.url == model.source.spec_url == MCP_URL
    assert model.source.spec_file == "source/mcp.json"
    assert model.real == RealTarget(base_url="https://mcp.deepwiki.test")
    assert model.mcp is not None
    assert [tool.name for tool in model.mcp.tools] == ["ask_question", "read_wiki_structure", "now"]
    snapshot = json.loads((layout.source_dir / "mcp.json").read_text())
    assert snapshot["initialize"]["serverInfo"]["name"] == "DeepWiki"
    assert [tool["name"] for tool in snapshot["tools"]] == [t.name for t in model.mcp.tools]
    progress = read_model(layout.progress_path, Progress)
    assert [e.endpoint for e in progress.endpoints] == [
        "MCP tools/call:ask_question",
        "MCP tools/call:read_wiki_structure",
        "MCP tools/call:now",
    ]
    assert any(layout.recordings.rglob("*.json"))
    assert "MCP server **DeepWiki**" in result.summary
    assert "`ask_question` — Ask a question about a GitHub repository." in result.summary
    assert result.notes == ["3 tools, 0 resources, 0 prompts"]
    assert "3 tools, 0 resources" not in result.summary  # notes are printed once, after it
    assert stat.S_IMODE(layout.env_file.stat().st_mode) == 0o600


async def test_mcp_credentials_are_used_for_discovery_and_never_recorded(
    ws: Workspace, tmp_path: Path
) -> None:
    token = "tok-0123456789-abcdef"
    model = mcp_model(auth=AuthModel(schemes=[AuthScheme(kind="bearer", prefix="Bearer ")]))
    app, _ = mcp_app(tmp_path / "server", model)
    headers = {"Authorization": f"Bearer {token}"}
    async with client(httpx2.ASGITransport(app=app)) as http:
        result = await init_fake(MCP_URL, workspace=ws, http=http, mcp_headers=headers, rps=10_000)
        assert "authenticate" not in repr(http.event_hooks)  # the hook is gone afterwards
    assert result.model.kind == "mcp"
    text = everything_under(result.layout.root)
    assert token not in text
    assert "[REDACTED]" in everything_under(result.layout.recordings)


async def test_without_credentials_a_protected_mcp_server_is_not_found(
    ws: Workspace, tmp_path: Path
) -> None:
    model = mcp_model(auth=AuthModel(schemes=[AuthScheme(kind="bearer", prefix="Bearer ")]))
    app, _ = mcp_app(tmp_path / "server", model)
    async with client(httpx2.ASGITransport(app=app)) as http:
        with pytest.raises(Exception, match="authorization required"):
            await init_fake(MCP_URL, workspace=ws, http=http, rps=10_000)


async def test_forced_mcp_skips_discovery(
    ws: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_discovery(*_: object) -> DiscoveredSource:
        raise AssertionError("discovery must not run with force_mcp")

    monkeypatch.setattr(init_module, "discover", no_discovery)
    app, _ = mcp_app(tmp_path / "server", mcp_model())
    async with client(httpx2.ASGITransport(app=app)) as http:
        result = await init_fake(
            MCP_URL, workspace=ws, http=http, force_mcp=True, name="wiki", rps=10_000
        )
    assert result.layout.name == "wiki"
    assert result.model.kind == "mcp"


async def test_mcp_introspection_respects_the_call_budget(ws: Workspace, tmp_path: Path) -> None:
    app, _ = mcp_app(tmp_path / "server", mcp_model())
    async with client(httpx2.ASGITransport(app=app)) as http:
        with pytest.raises(Exception, match="budget"):
            await init_fake(
                MCP_URL, workspace=ws, http=http, force_mcp=True, max_calls=1, rps=10_000
            )


async def test_mcp_discovery_follows_a_redirect_to_the_real_endpoint(
    ws: Workspace, tmp_path: Path
) -> None:
    model = mcp_model()
    assert model.mcp is not None
    model.mcp.transport.path = "/mcp/"
    model.routing.trailing_slash = "redirect"
    app, _ = mcp_app(tmp_path / "server", model)
    async with client(httpx2.ASGITransport(app=app)) as http:
        result = await init_fake(MCP_URL, workspace=ws, http=http, rps=10_000)
    assert result.model.source.url == MCP_URL
    assert result.model.source.spec_url == MCP_URL + "/"
    assert result.model.mcp is not None
    assert result.model.mcp.transport.path == "/mcp/"


async def test_mcp_server_on_the_legacy_sse_transport(ws: Workspace, tmp_path: Path) -> None:
    transport = McpTransportModel(kind="sse", path="/sse", messages_path="/messages/")
    app, _ = mcp_app(tmp_path / "server", mcp_model(transport=transport))
    async with live_server(app) as base, httpx2.AsyncClient(timeout=10) as http:
        result = await init_fake(f"{base}/sse", workspace=ws, http=http, name="legacy", rps=10_000)
    assert result.model.source.spec_url == f"{base}/sse"
    assert result.model.mcp is not None
    assert result.model.mcp.transport.kind == "sse"
    assert [tool.name for tool in result.model.mcp.tools] == [
        "ask_question",
        "read_wiki_structure",
        "now",
    ]


async def test_mcp_title_and_descriptions_fall_back_gracefully(
    ws: Workspace, tmp_path: Path
) -> None:
    long = "Explains things.\n" + "word " * 60
    model = mcp_model(
        tools=[
            McpToolModel(definition={"name": "explain", "description": long}),
            McpToolModel(definition={"name": "bare"}),
        ]
    )
    assert model.mcp is not None
    model.mcp.initialize_result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
    app, _ = mcp_app(tmp_path / "server", model)
    async with client(httpx2.ASGITransport(app=app)) as http:
        result = await init_fake(MCP_URL, workspace=ws, http=http, force_mcp=True, rps=10_000)
    assert result.model.title == "mcp.deepwiki.test"
    line = next(line for line in result.summary.splitlines() if "`explain`" in line)
    assert line.endswith("…")
    assert len(line) < 150
    assert "- `bare` — " in result.summary


def test_mcp_access_defaults_match_the_recorder() -> None:
    access = McpAccess()
    assert (access.headers, access.rps, access.max_calls) == ({}, 2.0, 2000)


# -- helpers --------------------------------------------------------------------------------------


def test_origin() -> None:
    assert origin("https://api.example.com:8443/v1/x?y=1") == "https://api.example.com:8443"


def test_header_secrets_include_the_bare_credential() -> None:
    values = ["Bearer abc123", "plain-key", "Bearer abc123", "Basic "]
    assert header_secrets(values) == ["Bearer abc123", "abc123", "plain-key", "Basic "]


def test_ensure_env_creates_a_private_file_once(ws: Workspace) -> None:
    layout = ws.fake("demo")
    assert env_value(layout, "FAUXCALL_PSEUDONYM_KEY") is None
    assert ensure_env(layout, "https://api.example.com") is True
    first = layout.env_file.read_text()
    assert stat.S_IMODE(layout.env_file.stat().st_mode) == 0o600
    assert ensure_env(layout, "https://other.example.com") is False
    assert layout.env_file.read_text() == first
    assert env_value(layout, "FAUXCALL_REAL__BASE_URL") == "https://api.example.com"
    assert env_value(layout, "MISSING") is None


def test_ensure_env_completes_a_prepared_file(ws: Workspace) -> None:
    layout = ws.fake("demo")
    layout.env_file.parent.mkdir(parents=True)
    layout.env_file.write_text('FAUXCALL_REAL__HEADERS=\'{"X-API-Key": "k"}\'')  # no newline
    assert ensure_env(layout, "https://api.example.com") is True
    assert env_value(layout, "FAUXCALL_REAL__HEADERS") == '{"X-API-Key": "k"}'  # kept
    assert env_value(layout, "FAUXCALL_REAL__BASE_URL") == "https://api.example.com"
    key = env_value(layout, "FAUXCALL_PSEUDONYM_KEY")
    assert key
    assert stat.S_IMODE(layout.env_file.stat().st_mode) == 0o600
    assert ensure_env(layout, "https://other.example.com") is False  # complete: untouched
    assert env_value(layout, "FAUXCALL_PSEUDONYM_KEY") == key


def test_create_private_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "secret"
    assert init_module._create_private(path, "one") is True
    assert init_module._create_private(path, "two") is False
    assert path.read_text() == "one"


def test_gitignore_is_completed_never_replaced(ws: Workspace) -> None:
    ws.root.mkdir(parents=True)
    gitignore = ws.root / ".gitignore"
    gitignore.write_text("node_modules/\n.env")
    assert ensure_workspace_gitignore(ws) is True
    lines = gitignore.read_text().splitlines()
    assert lines[:2] == ["node_modules/", ".env"]
    assert ".env.*" in lines
    assert "!.env.example" in lines
    assert lines.count(".env") == 1
    assert ensure_workspace_gitignore(ws) is False


def test_gitignore_is_created_from_the_template(ws: Workspace) -> None:
    ws.root.mkdir(parents=True)
    assert ensure_workspace_gitignore(ws) is True
    assert (ws.root / ".gitignore").read_text() == default_gitignore()
    assert ensure_workspace_gitignore(ws) is False


def test_an_empty_gitignore_gets_the_required_lines(ws: Workspace) -> None:
    ws.root.mkdir(parents=True)
    (ws.root / ".gitignore").write_text("")
    assert ensure_workspace_gitignore(ws) is True
    assert set(GITIGNORE_REQUIRED) <= set((ws.root / ".gitignore").read_text().splitlines())


def recorder_transport(seen: list[tuple[str, str | None]]) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx2.Response(200, text="ok")

    return httpx2.MockTransport(handler)


async def test_target_credentials_only_reach_the_target_host() -> None:
    seen: list[tuple[str, str | None]] = []
    async with client(recorder_transport(seen)) as http:
        async with target_credentials(http, "mcp.example.com/mcp", {"Authorization": "Bearer t"}):
            await http.get("https://mcp.example.com/mcp")
            await http.get("https://elsewhere.example.com/spec.json")
            await http.get("https://mcp.example.com/x", headers={"Authorization": "Bearer mine"})
        await http.get("https://mcp.example.com/mcp")
    assert seen == [
        ("https://mcp.example.com/mcp", "Bearer t"),
        ("https://elsewhere.example.com/spec.json", None),
        ("https://mcp.example.com/x", "Bearer mine"),
        ("https://mcp.example.com/mcp", None),
    ]


async def test_target_credentials_without_headers_change_nothing() -> None:
    seen: list[tuple[str, str | None]] = []
    async with client(recorder_transport(seen)) as http:
        hooks: Callable[[], object] = lambda: http.event_hooks["request"]  # noqa: E731
        before = list(hooks())  # type: ignore[call-overload]
        async with target_credentials(http, "https://mcp.example.com/mcp", {}):
            assert list(hooks()) == before  # type: ignore[call-overload]
            await http.get("https://mcp.example.com/mcp")
    assert seen == [("https://mcp.example.com/mcp", None)]
