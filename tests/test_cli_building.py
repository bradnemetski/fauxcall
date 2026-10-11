"""CLI: world build/check/expand, scaffold and report."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from fauxcall.build.scaffold import ScaffoldResult
from fauxcall.cli import building
from fauxcall.cli.main import app
from fauxcall.model.api import ApiModel, RealTarget, SourceInfo
from fauxcall.model.endpoints import EndpointModel
from fauxcall.model.fields import IgnorePolicy, VolatilePolicy
from fauxcall.model.mcp import McpModel, McpToolModel
from fauxcall.model.progress import Progress
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange
from fauxcall.model.world import CollectionModel, ExpansionSpec, RelationModel
from fauxcall.paths import Workspace
from fauxcall.recording.store import RecordingStore
from fauxcall.storage import write_json, write_model
from fauxcall.types import HttpMethod
from fauxcall.world.io import load_collection

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from typer.testing import Result

    from fauxcall.paths import FakeLayout
    from fauxcall.types import JsonValue


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    for variable in list(os.environ):
        if variable.startswith("FAUXCALL_") or variable.endswith("TYPESAFE_API_KEY"):
            monkeypatch.delenv(variable)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAUXCALL_WORKSPACE", str(tmp_path / "fakes"))
    return Workspace(tmp_path / "fakes")


def invoke(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == code, (result.output, result.exception)
    return result


def get(path: str) -> EndpointModel:
    return EndpointModel(id=f"GET {path}", method=HttpMethod.GET, path=path)


def shop_model(**updates: object) -> ApiModel:
    model = ApiModel(
        name="shop",
        title="Shop",
        source=SourceInfo(
            kind="openapi", url="https://shop.test/openapi.json", fetched_at=datetime.now(UTC)
        ),
        real=RealTarget(base_url="https://shop.test", base_path="/v1"),
        endpoints=[get("/v1/items"), get("/v1/users")],
        collections=[
            CollectionModel(
                name="items", sources=["GET /v1/items"], extract={"GET /v1/items": "$[*]"}
            ),
            CollectionModel(
                name="users", sources=["GET /v1/users"], extract={"GET /v1/users": "$[*]"}
            ),
        ],
        relations=[
            RelationModel(
                name="item-owner",
                from_collection="items",
                from_field="owner",
                to_collection="users",
            )
        ],
    )
    return model.model_copy(update=updates)


def make_fake(ws: Workspace, model: ApiModel) -> FakeLayout:
    fake = ws.fake(model.name)
    write_model(fake.model_path, model)
    now = datetime.now(UTC)
    progress = Progress(fake=model.name, source=model.source.url, started_at=now, updated_at=now)
    for endpoint in model.endpoints:
        progress.endpoint(endpoint.id)
    write_model(fake.progress_path, progress)
    return fake


def save_exchange(fake: FakeLayout, endpoint: str, body: JsonValue, status: int = 200) -> None:
    store = RecordingStore(fake.recordings)
    exchange_id = store.next_id()
    path = endpoint.split(" ", 1)[1]
    store.save(
        Exchange(
            id=exchange_id,
            endpoint=endpoint,
            started_at=datetime.now(UTC),
            started_ns=int(exchange_id),
            request=CapturedRequest(
                method=HttpMethod.GET, url=f"https://shop.test{path}", path=path
            ),
            response=CapturedResponse(status=status, body=Body(kind="json", json_value=body)),
        )
    )


def record_shop(ws: Workspace, *, owners: tuple[int, ...] = (10, 11)) -> FakeLayout:
    fake = make_fake(ws, shop_model())
    items: JsonValue = [{"id": i, "owner": owner} for i, owner in enumerate(owners, start=1)]
    save_exchange(fake, "GET /v1/items", items)
    save_exchange(fake, "GET /v1/users", [{"id": 10}, {"id": 11}])
    save_exchange(fake, "GET /v1/users", {"error": "busy"}, status=503)
    return fake


# -- world ----------------------------------------------------------------------------------------


def test_world_build_writes_consistent_collections(ws: Workspace) -> None:
    fake = record_shop(ws)
    output = invoke("world", "build", "shop").output
    assert "| items | 2 | 1 | 0 | 0 |" in output
    assert "| users | 2 | 1 | 0 | 0 |" in output
    assert output.rstrip().endswith("world is consistent")
    assert [r.key for r in load_collection(fake.data / "world/items.jsonl")] == [1, 2]
    assert "world is consistent" in invoke("world", "check", "shop").output


def test_world_build_reports_dangling_references(ws: Workspace) -> None:
    record_shop(ws, owners=(10, 99))
    result = invoke("world", "build", "shop", code=1)
    assert "Consistency problems" in result.output
    assert "99" in result.output
    assert "Consistency problems" in invoke("world", "check", "shop", code=1).output


def test_world_build_needs_collections(ws: Workspace) -> None:
    make_fake(ws, shop_model(collections=[], relations=[]))
    assert "the model has no collections yet" in invoke("world", "build", "shop", code=1).output


def test_world_expand(ws: Workspace) -> None:
    fake = record_shop(ws)
    invoke("world", "build", "shop")
    assert (
        "has no 'expansion' settings" in invoke("world", "expand", "shop", "items", code=1).output
    )
    assert "unknown collection 'nope'" in invoke("world", "expand", "shop", "nope", code=1).output
    model = shop_model()
    model.collections[0].expansion = ExpansionSpec(target_count=5, vary=["owner"])
    write_model(fake.model_path, model)
    output = invoke("world", "expand", "shop", "items").output
    assert output.startswith("items: 2 recorded + 3 generated")
    assert "world is consistent" in output
    records = load_collection(fake.data / "world/items.jsonl")
    assert len(records) == 5
    assert {r.record["owner"] for r in records if isinstance(r.record, dict)} <= {10, 11}
    again = invoke("world", "expand", "shop", "items").output
    assert again.startswith("items: 2 recorded + 3 generated")


# -- scaffold -------------------------------------------------------------------------------------


def test_scaffold_generates_the_package(ws: Workspace, tmp_path: Path) -> None:
    fake = record_shop(ws)
    output = invoke("scaffold", "shop").output
    assert "- wrote `fakes/shop/pyproject.toml`" in output
    assert f"Next: `cd {fake.root} && uv sync && uv run pytest`" in output
    assert (fake.src / "routes.py").exists()
    assert (fake.tests / "unit" / "test_app.py").exists()
    assert (ws.root / "compose.yaml").exists()
    assert (tmp_path / ".github" / "workflows" / "fauxcall.yml").exists()
    again = invoke("scaffold", "shop").output
    assert "- kept `fakes/shop/pyproject.toml` (yours)" in again


def test_scaffold_passes_recordings_root_and_source(
    ws: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = record_shop(ws)
    seen: list[tuple[FakeLayout, Workspace, ApiModel, list[Exchange], Path | None, str | None]] = []

    def generate(
        layout: FakeLayout,
        workspace: Workspace,
        model: ApiModel,
        *,
        exchanges: Sequence[Exchange] = (),
        project_root: Path | None = None,
        source: str | None = None,
    ) -> ScaffoldResult:
        seen.append((layout, workspace, model, list(exchanges), project_root, source))
        return ScaffoldResult(written=[fake.src / "routes.py"], kept=[fake.root / "pyproject.toml"])

    monkeypatch.setattr(building, "generate", generate)
    monkeypatch.setenv("FAUXCALL_SOURCE", "/checkout/fauxcall")
    output = invoke("scaffold", "shop").output
    assert "- wrote `fakes/shop/src/fauxcall_shop/routes.py`" in output
    assert "- kept `fakes/shop/pyproject.toml` (yours)" in output
    [(layout, workspace, model, exchanges, project_root, source)] = seen
    assert layout == fake
    assert workspace == ws
    assert model.name == "shop"
    assert [e.endpoint for e in exchanges] == ["GET /v1/items", "GET /v1/users", "GET /v1/users"]
    assert project_root == tmp_path
    assert source == "/checkout/fauxcall"


def test_scaffold_with_the_default_relative_workspace(
    ws: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_shop(ws)
    monkeypatch.setenv("FAUXCALL_WORKSPACE", "fakes")
    output = invoke("scaffold", "shop").output
    assert "- wrote `fakes/shop/src/fauxcall_shop/routes.py`" in output
    assert "Next: `cd fakes/shop && uv sync && uv run pytest`" in output


def test_scaffold_with_a_project_root(ws: Workspace, tmp_path: Path) -> None:
    record_shop(ws)
    project = tmp_path / "project"
    project.mkdir()
    invoke("scaffold", "shop", "--project-root", str(project))
    assert (project / ".github" / "workflows" / "fauxcall.yml").exists()


def test_scaffold_unknown_fake(ws: Workspace) -> None:
    assert "no fake called 'shop'" in invoke("scaffold", "shop", code=1).output


# -- report ---------------------------------------------------------------------------------------


def test_report_without_parity(ws: Workspace) -> None:
    fake = record_shop(ws)
    model = shop_model()
    model.endpoints[0].fields = [VolatilePolicy(path="$.price"), IgnorePolicy(path="$.etag")]
    model.endpoints[1].collection = "missing"
    write_model(fake.model_path, model)
    progress = Progress(
        fake="shop", source="x", started_at=datetime.now(UTC), updated_at=datetime.now(UTC)
    )
    progress.endpoint("GET /v1/items").notes.append("needs a sort order")
    progress.endpoint("GET /v1/users")
    write_model(fake.progress_path, progress)
    text = invoke("report", "shop").output
    assert text.startswith("# Shop — FauxCall fake `shop`")
    assert (
        "Source: https://shop.test/openapi.json (openapi) · real API: https://shop.test/v1" in text
    )
    assert "- endpoints: 2 (replay 2)" in text
    assert "- recordings: 3 exchanges" in text
    assert "- collections: items, users; relations: 1" in text
    assert "- field policies: volatile 1, ignore 1" in text
    assert "No parity run yet" in text
    assert "- model: endpoint 'GET /v1/users': unknown collection 'missing'" in text
    assert "- GET /v1/items: needs a sort order" in text
    assert "GET /v1/users:" not in text
    assert (fake.reports / "summary.md").read_text() == text.rstrip("\n") + "\n"


def test_report_with_parity_results(ws: Workspace) -> None:
    fake = make_fake(ws, shop_model(collections=[], relations=[], endpoints=[]))
    write_json(fake.reports / "parity.json", {"score": 0.991, "threshold": 0.98, "ok": True})
    text = invoke("report", "shop").output
    assert "Compliance **99.10%** (threshold 98%) — PASS." in text
    assert "- endpoints: 0 (n/a)" in text
    assert "- collections: none; relations: 0" in text
    assert "- field policies: none" in text
    assert "- none in the model" in text
    write_json(fake.reports / "parity.json", {"score": 0.5, "threshold": 0.98, "ok": False})
    assert "— FAIL." in invoke("report", "shop").output
    (fake.reports / "parity.json").write_text("{broken")
    assert invoke("report", "shop", code=1).output.startswith("error:")


def test_report_for_an_mcp_fake(ws: Workspace) -> None:
    model = ApiModel(
        name="demo",
        title="Demo",
        kind="mcp",
        source=SourceInfo(kind="mcp", url="http://mcp.test/mcp", fetched_at=datetime.now(UTC)),
        real=RealTarget(base_url="http://mcp.test"),
        mcp=McpModel(
            tools=[McpToolModel(definition={"name": "ask"})],
            resources=[{"uri": "file:///a"}],
        ),
    )
    make_fake(ws, model)
    text = invoke("report", "demo").output
    assert "- MCP tools: 1 · resources: 1 · prompts: 0" in text
    assert json.loads(json.dumps(text))
