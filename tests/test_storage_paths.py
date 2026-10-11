from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from fauxcall.errors import ConfigurationError
from fauxcall.jsonfmt import JsonFloat
from fauxcall.paths import (
    FakeLayout,
    Workspace,
    find_workspace,
    package_name,
    slugify,
    validate_slug,
)
from fauxcall.storage import (
    append_jsonl,
    atomic_write_text,
    read_json,
    read_jsonl,
    read_model,
    write_json,
    write_jsonl,
    write_model,
)


class _Thing(BaseModel):
    name: str
    count: int = 0
    tags: list[str] = []


def test_json_round_trip_preserves_spelling(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "value.json"
    write_json(path, {"price": JsonFloat("105.50"), "items": [1, 2]})
    assert path.read_text() == '{\n  "price": 105.50,\n  "items": [\n    1,\n    2\n  ]\n}\n'
    value = read_json(path)
    assert isinstance(value, dict)
    price = value["price"]
    assert isinstance(price, JsonFloat)
    assert price.lexeme == "105.50"


def test_atomic_write_leaves_no_temp_files_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "first")

    def broken_replace(self: Path, other: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(target, "second")
    assert target.read_text() == "first"
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_model_round_trip_omits_defaults(tmp_path: Path) -> None:
    path = tmp_path / "thing.json"
    write_model(path, _Thing(name="x", tags=["a"]))
    assert '"count"' not in path.read_text()
    assert read_model(path, _Thing) == _Thing(name="x", tags=["a"])


def test_jsonl_round_trip_append_and_errors(tmp_path: Path) -> None:
    path = tmp_path / "data" / "items.jsonl"
    assert list(read_jsonl(path)) == []
    write_jsonl(path, [{"a": 1}, {"b": JsonFloat("2.50")}])
    append_jsonl(path, {"c": 3})
    path.write_text(path.read_text() + "\n")
    assert list(read_jsonl(path)) == [{"a": 1}, {"b": 2.5}, {"c": 3}]
    assert "2.50" in path.read_text()
    path.write_text('{"ok": 1}\nnot json\n')
    with pytest.raises(ValueError, match=r"items\.jsonl:2"):
        list(read_jsonl(path))


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Swagger Petstore - OpenAPI 3.0", "swagger-petstore-openapi-3-0"),
        ("FakeRESTApi.Web V1", "fakerestapi-web-v1"),
        ("  ", "api"),
        ("123 Numbers First", "numbers-first"),
        ("x" * 60, "x" * 40),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert slugify(title) == slug
    validate_slug(slug)


@pytest.mark.parametrize(
    "bad", ["Petstore", "pet_store", "-pet", "pet-", "pet--store", "1pet", "x" * 41]
)
def test_validate_slug_rejects(bad: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_slug(bad)


def test_layout_paths(tmp_path: Path) -> None:
    layout = Workspace(tmp_path / "fauxcall").fake("pet-store")
    assert layout.package == "fauxcall_pet_store" == package_name("pet-store")
    assert layout.data == tmp_path / "fauxcall" / "pet-store" / "data"
    expected = {
        layout.source_dir: "source",
        layout.recordings: "recordings",
        layout.model_path: "model.json",
        layout.world: "world",
        layout.series: "series",
        layout.plans: "plans",
        layout.approvals_path: "approvals.json",
        layout.progress_path: "progress.json",
        layout.jev_cache: "jev-cache.jsonl",
        layout.reports: "reports",
        layout.state: "state",
    }
    for path, name in expected.items():
        assert path == layout.data / name
    assert layout.src == layout.root / "src" / "fauxcall_pet_store"
    assert layout.tests == layout.root / "tests"
    assert layout.env_file == layout.root / ".env"
    assert not layout.exists()


def test_workspace_lists_started_fakes_only(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fauxcall")
    assert workspace.fakes() == []
    started = workspace.fake("alpha")
    write_json(started.progress_path, {})
    (workspace.root / "beta" / "data").mkdir(parents=True)  # no model/progress yet
    (workspace.root / "Not_A_Slug" / "data").mkdir(parents=True)
    (workspace.root / "file.txt").write_text("")
    assert workspace.fakes() == [FakeLayout(root=started.root, name="alpha")]


def _project_with_a_fake(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    model = root / "fauxcall" / "petstore" / "data" / "model.json"
    model.parent.mkdir(parents=True)
    model.write_text("{}")
    return root / "fauxcall"


def test_find_workspace_from_anywhere_in_the_project(tmp_path: Path) -> None:
    workspace = _project_with_a_fake(tmp_path / "project")
    relative = Path("fauxcall")
    assert find_workspace(relative, tmp_path / "project") == workspace  # the project root
    assert find_workspace(relative, workspace) == workspace  # the workspace itself
    assert find_workspace(relative, workspace / "petstore" / "data") == workspace  # inside a fake
    deeper = tmp_path / "project" / "src" / "app"
    deeper.mkdir(parents=True)
    assert find_workspace(relative, deeper) == workspace
    assert find_workspace(tmp_path / "elsewhere", deeper) == tmp_path / "elsewhere"  # absolute


def test_find_workspace_never_leaves_the_project(tmp_path: Path) -> None:
    _project_with_a_fake(tmp_path)  # fakes above the project's own root must not be used
    project = tmp_path / "other"
    (project / ".git").mkdir(parents=True)
    assert find_workspace(Path("fauxcall"), project) == project / "fauxcall"
    empty = tmp_path / "other" / "plain" / "folder"
    empty.mkdir(parents=True)
    (tmp_path / "other" / "plain" / "fauxcall").mkdir()  # a folder of that name without fakes
    assert find_workspace(Path("fauxcall"), empty) == empty / "fauxcall"
