from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from structlog.testing import capture_logs

from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange
from fauxcall.runtime.recordings import RecordingIndex
from fauxcall.storage import write_model

if TYPE_CHECKING:
    from pathlib import Path

AT = datetime(2026, 10, 10, 14, 30, tzinfo=UTC)
PETS = "GET /v1/pets/{petId}"
UPLOAD = "GET /v1/uploads/{id}/content"


def exchange(
    exchange_id: str, endpoint: str, started_ns: int, body: Body | None = None
) -> Exchange:
    method, path = endpoint.split(" ", 1)
    return Exchange(
        id=exchange_id,
        endpoint=endpoint,
        started_at=AT,
        started_ns=started_ns,
        request=CapturedRequest.model_validate(
            {"method": method, "url": f"https://api.test{path}", "path": path}
        ),
        response=CapturedResponse(status=200, body=body or Body()),
    )


def save(root: Path, slug: str, item: Exchange) -> Path:
    path = root / slug / f"{item.id}.json"
    write_model(path, item)
    return path


def test_loads_lazily_and_only_once(tmp_path: Path) -> None:
    index = RecordingIndex(tmp_path)
    save(tmp_path, "pets", exchange("ex-1", PETS, 10))  # written after the index was created
    assert [e.id for e in index.for_endpoint(PETS)] == ["ex-1"]
    save(tmp_path, "pets", exchange("ex-2", PETS, 20))  # written after the first load
    assert [e.id for e in index.for_endpoint(PETS)] == ["ex-1"]


def test_groups_by_endpoint_ordered_by_time_then_id(tmp_path: Path) -> None:
    save(tmp_path, "pets", exchange("a-late", PETS, 300))
    save(tmp_path, "pets", exchange("b-early", PETS, 100))
    save(tmp_path, "pets", exchange("c-tie", PETS, 200))
    save(tmp_path, "pets", exchange("a-tie", PETS, 200))
    save(tmp_path, "uploads/nested", exchange("up-1", UPLOAD, 5))
    index = RecordingIndex(tmp_path)
    assert [e.id for e in index.for_endpoint(PETS)] == ["b-early", "a-tie", "c-tie", "a-late"]
    assert index.endpoints() == [PETS, UPLOAD]
    assert index.for_endpoint("GET /nothing") == []
    found = index.get("up-1")
    assert found is not None
    assert found.endpoint == UPLOAD
    assert index.get("missing") is None


def test_for_endpoint_returns_a_copy(tmp_path: Path) -> None:
    save(tmp_path, "pets", exchange("ex-1", PETS, 1))
    index = RecordingIndex(tmp_path)
    index.for_endpoint(PETS).clear()
    assert len(index.for_endpoint(PETS)) == 1


def test_unreadable_files_are_skipped_with_a_warning(tmp_path: Path) -> None:
    save(tmp_path, "pets", exchange("good", PETS, 1))
    (tmp_path / "pets" / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "pets" / "invalid.json").write_text('{"id": "x"}', encoding="utf-8")
    (tmp_path / "pets" / "latin1.json").write_bytes(b'{"id": "\xe9"}')
    (tmp_path / "pets" / "folder.json").mkdir()
    (tmp_path / "pets" / "good.response.bin").write_bytes(b"\x00\x01")
    index = RecordingIndex(tmp_path)
    with capture_logs() as logs:
        assert [e.id for e in index.for_endpoint(PETS)] == ["good"]
    warned = sorted(
        str(entry["path"]).rsplit("/", 1)[-1]
        for entry in logs
        if entry["event"] == "recording_unreadable"
    )
    assert warned == ["broken.json", "invalid.json", "latin1.json"]


def test_missing_directory_means_no_recordings(tmp_path: Path) -> None:
    index = RecordingIndex(tmp_path / "recordings")
    assert index.endpoints() == []
    assert index.get("x") is None


def test_binary_sidecars(tmp_path: Path) -> None:
    body = Body(kind="binary", content_type="application/pdf", file="up-1.response.bin", size=4)
    item = exchange("up-1", UPLOAD, 1, body)
    save(tmp_path, "uploads", item)
    (tmp_path / "uploads" / "up-1.response.bin").write_bytes(b"%PDF")
    gone = exchange("up-2", UPLOAD, 2, Body(kind="binary", file="up-2.response.bin"))
    save(tmp_path, "uploads", gone)
    folder = exchange("up-3", UPLOAD, 3, Body(kind="binary", file="sidecar-dir"))
    save(tmp_path, "uploads", folder)
    (tmp_path / "uploads" / "sidecar-dir").mkdir()
    index = RecordingIndex(tmp_path)
    loaded = index.get("up-1")
    assert loaded is not None
    assert loaded.response is not None
    assert index.binary(loaded, loaded.response.body) == b"%PDF"
    with capture_logs() as logs:
        assert index.binary(gone, Body(kind="binary", file="up-2.response.bin")) is None
        assert index.binary(folder, Body(kind="binary", file="sidecar-dir")) is None
    assert [entry["event"] for entry in logs] == ["recording_sidecar_missing"] * 2
    assert index.binary(loaded, Body(kind="json")) is None
    stranger = exchange("not-indexed", UPLOAD, 9, body)
    assert index.binary(stranger, body) is None
