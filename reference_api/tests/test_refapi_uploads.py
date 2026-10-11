from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from fauxcall_reference_api.routes.uploads import content_disposition

from .helpers import DEMO, SECOND, error_of

if TYPE_CHECKING:
    from starlette.testclient import TestClient

    from fauxcall_reference_api.clock import ManualClock

CREATED = "2026-10-09T15:00:00+00:00"
COMPLETED = "2026-10-09T15:00:08+00:00"


def upload(
    client: TestClient,
    content: bytes = b"hello world",
    *,
    filename: str = "notes.txt",
    content_type: str = "text/plain",
    label: str | None = None,
) -> dict[str, object]:
    data = {"label": label} if label is not None else None
    response = client.post(
        "/v1/uploads", headers=DEMO, files={"file": (filename, content, content_type)}, data=data
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_create(client: TestClient) -> None:
    body = upload(client, label="quarterly")
    assert uuid.UUID(str(body["id"])).version == 4
    assert body == {
        "id": body["id"],
        "status": "queued",
        "filename": "notes.txt",
        "size": 11,
        "label": "quarterly",
        "created_at": CREATED,
        "progress": 0,
    }
    assert list(body) == ["id", "status", "filename", "size", "label", "created_at", "progress"]
    assert upload(client)["label"] is None


@pytest.mark.parametrize(
    ("elapsed_ns", "status", "progress"),
    [
        (0, "queued", 0),
        (2 * SECOND - 1, "queued", 0),
        (2 * SECOND, "processing", 0),
        (2 * SECOND + 60_000_000, "processing", 1),
        (5 * SECOND, "processing", 50),
        (8 * SECOND - 1, "processing", 99),
        (8 * SECOND, "done", 100),
        (3600 * SECOND, "done", 100),
    ],
)
def test_status_progression(
    client: TestClient, clock: ManualClock, elapsed_ns: int, status: str, progress: int
) -> None:
    created = upload(client)
    clock.set_ns(clock.now_ns() + elapsed_ns)
    body = client.get(f"/v1/uploads/{created['id']}", headers=DEMO).json()
    assert (body["status"], body["progress"]) == (status, progress)
    assert body["created_at"] == CREATED
    if status == "done":
        assert body["completed_at"] == COMPLETED
        assert "error" not in body
    else:
        assert "completed_at" not in body


def test_corrupt_files_fail(client: TestClient, clock: ManualClock) -> None:
    created = upload(client, b"header\nCORRUPT\ntrailer")
    clock.advance(7.999)
    assert client.get(f"/v1/uploads/{created['id']}", headers=DEMO).json()["status"] == "processing"
    clock.advance(0.001)
    body = client.get(f"/v1/uploads/{created['id']}", headers=DEMO).json()
    assert body == {
        "id": created["id"],
        "status": "failed",
        "filename": "notes.txt",
        "size": 22,
        "label": None,
        "created_at": CREATED,
        "progress": 100,
        "completed_at": COMPLETED,
        "error": "corrupt_file",
    }
    content = client.get(f"/v1/uploads/{created['id']}/content", headers=DEMO)
    assert content.status_code == 409
    assert error_of(content.json())["message"] == (
        f"Upload '{created['id']}' is failed; content is available only when status is done."
    )


def test_lowercase_corrupt_is_fine(client: TestClient, clock: ManualClock) -> None:
    created = upload(client, b"corrupt")
    clock.advance(8)
    assert client.get(f"/v1/uploads/{created['id']}", headers=DEMO).json()["status"] == "done"


def test_content_only_when_done(client: TestClient, clock: ManualClock) -> None:
    payload = bytes(range(256))
    created = upload(client, payload, filename="naïve file.bin", content_type="application/x-thing")
    for advance, status in [(0, "queued"), (3, "processing")]:
        clock.advance(advance)
        response = client.get(f"/v1/uploads/{created['id']}/content", headers=DEMO)
        assert response.status_code == 409
        error = error_of(response.json())
        assert error["code"] == "not_ready"
        assert f"is {status};" in error["message"]
    clock.advance(5)
    response = client.get(f"/v1/uploads/{created['id']}/content", headers=DEMO)
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == "application/x-thing"
    assert response.headers["content-disposition"] == 'attachment; filename="na_ve file.bin"'
    assert response.headers["content-length"] == "256"


def test_text_content_type_is_returned_verbatim(client: TestClient, clock: ManualClock) -> None:
    created = upload(client, b"a,b\n", filename="data.csv", content_type="text/csv")
    clock.advance(8)
    response = client.get(f"/v1/uploads/{created['id']}/content", headers=DEMO)
    assert response.headers["content-type"] == "text/csv"


def test_missing_content_type_defaults_to_octet_stream(
    client: TestClient, clock: ManualClock
) -> None:
    boundary = "XyZ"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename=""\r\n'
        "\r\n"
        "raw bytes\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    response = client.post(
        "/v1/uploads",
        headers={**DEMO, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=body,
    )
    assert response.status_code == 202
    created = response.json()
    assert created["filename"] == ""
    clock.advance(8)
    download = client.get(f"/v1/uploads/{created['id']}/content", headers=DEMO)
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"] == 'attachment; filename=""'


def test_size_limit(client: TestClient) -> None:
    exactly = upload(client, b"x" * 1_048_576)
    assert exactly["size"] == 1_048_576
    response = client.post(
        "/v1/uploads", headers=DEMO, files={"file": ("big.bin", b"x" * 1_048_577)}
    )
    assert response.status_code == 413
    assert error_of(response.json()) == {
        "code": "payload_too_large",
        "message": "File exceeds the 1 MiB (1048576 bytes) limit.",
        "request_id": response.json()["error"]["request_id"],
    }
    assert len(client.get("/v1/uploads", headers=DEMO).json()) == 1


def test_missing_or_non_file_part(client: TestClient) -> None:
    for response in (
        client.post("/v1/uploads", headers=DEMO),
        client.post("/v1/uploads", headers=DEMO, json={"file": "x"}),
        client.post("/v1/uploads", headers=DEMO, data={"label": "no file"}),
    ):
        assert response.status_code == 400
        error = error_of(response.json())
        assert (error["code"], error["param"]) == ("missing_parameter", "file")
        assert error["message"] == "Missing required parameter 'file'."
    text_part = client.post("/v1/uploads", headers=DEMO, data={"file": "not a file"})
    assert error_of(text_part.json()) == {
        "code": "invalid_parameter",
        "message": "Invalid value for parameter 'file': must be a file.",
        "request_id": text_part.json()["error"]["request_id"],
        "param": "file",
    }


def test_list_newest_first_and_delete(client: TestClient, clock: ManualClock) -> None:
    first = upload(client, filename="a.txt")
    clock.advance(1)
    second = upload(client, filename="b.txt")
    third = upload(client, filename="c.txt")  # same instant as the second: creation order decides
    clock.advance(7.5)
    listed = client.get("/v1/uploads", headers=DEMO).json()
    assert [u["id"] for u in listed] == [third["id"], second["id"], first["id"]]
    assert [u["status"] for u in listed] == ["processing", "processing", "done"]
    assert "completed_at" not in listed[0]
    assert listed[2]["completed_at"] == COMPLETED
    response = client.delete(f"/v1/uploads/{second['id']}", headers=DEMO)
    assert response.status_code == 204
    assert response.content == b""
    for method, path in [
        ("GET", f"/v1/uploads/{second['id']}"),
        ("GET", f"/v1/uploads/{second['id']}/content"),
        ("DELETE", f"/v1/uploads/{second['id']}"),
    ]:
        missing = client.request(method, path, headers=DEMO)
        assert missing.status_code == 404
        assert error_of(missing.json())["message"] == f"No upload with id '{second['id']}'."
    assert len(client.get("/v1/uploads", headers=DEMO).json()) == 2


def test_content_disposition() -> None:
    assert content_disposition("report.pdf") == 'attachment; filename="report.pdf"'
    assert content_disposition('a"b\\cé\n.txt') == 'attachment; filename="a_b_c__.txt"'
