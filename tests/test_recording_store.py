from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fauxcall.model import Body, CapturedRequest, CapturedResponse, Exchange
from fauxcall.paths import endpoint_slug, unique_endpoint_slug
from fauxcall.recording.store import RecordingStore
from fauxcall.storage import read_model, write_model
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from pathlib import Path

AT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def make(endpoint: str, exchange_id: str, *, status: int = 200) -> Exchange:
    return Exchange(
        id=exchange_id,
        endpoint=endpoint,
        started_at=AT,
        started_ns=1_791_633_600_000_000_000,
        request=CapturedRequest(method=HttpMethod.GET, url="https://api.test/x", path="/x"),
        response=CapturedResponse(status=status, body=Body(kind="json", json_value={"ok": True})),
    )


def save(store: RecordingStore, endpoint: str, **kwargs: int) -> Exchange:
    exchange = make(endpoint, store.next_id(), **kwargs)
    store.save(exchange)
    return exchange


def test_ids_are_zero_padded_and_continue_across_restarts(tmp_path: Path) -> None:
    root = tmp_path / "recordings"
    store = RecordingStore(root)
    assert [store.next_id(), store.next_id()] == ["000001", "000002"]
    save(store, "GET /pets")  # 000003
    (root / "get-pets" / "notes.json").write_text("{}")  # not an exchange: ignored
    (root / "get-pets" / "000999.response.bin").write_bytes(b"\x00")
    assert RecordingStore(root).next_id() == "000004"
    write_model(root / "elsewhere" / "1000000.json", make("GET /far", "1000000"))
    assert RecordingStore(root).next_id() == "1000001"
    assert RecordingStore(tmp_path / "missing").next_id() == "000001"


def test_save_writes_json_and_sidecars(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path / "recordings")
    exchange = make("GET /files/{id}", store.next_id())
    name = store.sidecar_name(exchange.id, "response")
    assert name == "000001.response.bin"
    path = store.save(exchange, {name: b"\x89PNG"})
    assert path == tmp_path / "recordings" / "get-files-id" / "000001.json"
    assert (path.parent / name).read_bytes() == b"\x89PNG"
    assert read_model(path, Exchange) == exchange


@pytest.mark.parametrize("name", ["../escape.bin", "a/b.bin", ".hidden", "000002.json", ""])
def test_save_refuses_unsafe_sidecar_names(tmp_path: Path, name: str) -> None:
    store = RecordingStore(tmp_path)
    with pytest.raises(ValueError, match="invalid sidecar file name"):
        store.save(make("GET /x", "000001"), {name: b"x"})
    assert not (tmp_path.parent / "escape.bin").exists()


def test_iteration_is_in_numeric_id_order_across_directories(tmp_path: Path) -> None:
    root = tmp_path / "recordings"
    store = RecordingStore(root)
    first = save(store, "GET /b")
    second = save(store, "GET /a")
    third = save(store, "GET /b")
    write_model(root / "get-c" / "1000000.json", make("GET /c", "1000000"))
    (root / "get-a" / "readme.json").write_text("{}")
    ids = [exchange.id for _, exchange in store.iter_exchanges()]
    assert ids == [first.id, second.id, third.id, "1000000"]  # 999999 < 1000000, unlike names
    paths = [path.name for path, _ in store.iter_exchanges()]
    assert paths[-1] == "1000000.json"
    assert list(RecordingStore(tmp_path / "missing").iter_exchanges()) == []


def test_for_endpoint(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    one = save(store, "GET /pets/{id}", status=200)
    save(store, "GET /other")
    two = save(store, "GET /pets/{id}", status=404)
    assert [e.id for e in store.for_endpoint("GET /pets/{id}")] == [one.id, two.id]
    assert store.for_endpoint("GET /never") == []
    # A stray exchange of another endpoint in the same directory is not returned.
    write_model(store.directory("GET /pets/{id}") / "000050.json", make("GET /stray", "000050"))
    assert [e.id for e in store.for_endpoint("GET /pets/{id}")] == [one.id, two.id]


def test_slug_collisions_in_one_process(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    assert endpoint_slug("GET /a-b") == endpoint_slug("GET /a/b")
    first = store.directory("GET /a-b")  # claimed, nothing on disk yet
    second = store.directory("GET /a/b")
    assert first == tmp_path / endpoint_slug("GET /a-b")
    assert second == tmp_path / unique_endpoint_slug("GET /a/b")
    assert store.directory("GET /a-b") == first  # cached
    save(store, "GET /a/b")
    save(store, "GET /a-b")
    assert [e.endpoint for e in store.for_endpoint("GET /a/b")] == ["GET /a/b"]
    assert [e.endpoint for e in store.for_endpoint("GET /a-b")] == ["GET /a-b"]


def test_slug_collisions_across_restarts(tmp_path: Path) -> None:
    save(RecordingStore(tmp_path), "GET /a/b")  # owns the plain slug on disk
    store = RecordingStore(tmp_path)
    assert store.directory("GET /a/b") == tmp_path / "get-a-b"
    assert store.directory("GET /a-b") == tmp_path / unique_endpoint_slug("GET /a-b")
    fresh = RecordingStore(tmp_path)  # the order of first use doesn't matter
    assert fresh.directory("GET /a-b") == tmp_path / unique_endpoint_slug("GET /a-b")
    assert fresh.directory("GET /a/b") == tmp_path / "get-a-b"


def test_unreadable_files_do_not_decide_ownership(tmp_path: Path) -> None:
    directory = tmp_path / "get-a-b"
    directory.mkdir()
    (directory / "000001.json").write_text("{not json")
    store = RecordingStore(tmp_path)
    assert store.directory("GET /a-b") == directory  # no readable owner yet
    write_model(directory / "000002.json", make("GET /a/b", "000002"))
    assert RecordingStore(tmp_path).directory("GET /a-b") == (
        tmp_path / unique_endpoint_slug("GET /a-b")
    )


def test_delete_removes_the_exchange_and_its_sidecars(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    kept = store.save(make("GET /a", "000001"), {"000001.body.bin": b"keep"})
    gone = make("GET /a", "000002")
    path = store.save(gone, {"000002.body.bin": b"x", "000002.part1.bin": b"y"})
    store.delete(path, gone)
    left = sorted(p.name for p in path.parent.iterdir())
    assert left == ["000001.body.bin", kept.name]
