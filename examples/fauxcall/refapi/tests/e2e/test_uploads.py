"""Uploads: multipart writes processed as async jobs (writes: run with --allow-real-writes).

A job is queued for 2 s and processing for 6 s on both sides (the fake's clock runs in real
time here), so these scenarios poll; identical polls in a row count once in the comparison.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx2
    from fauxcall.testing import ApiClient

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8"
    "cf00000301010018dd8db00000000049454e44ae426082"
)


@contextlib.asynccontextmanager
async def _upload(
    api: ApiClient, name: str, content: bytes, content_type: str, **form: str
) -> AsyncIterator[dict[str, Any]]:
    response = await api.post(
        "/v1/uploads", files={"file": (name, content, content_type)}, data=form
    )
    assert response.status_code == 202, response.text
    upload: dict[str, Any] = response.json()
    try:
        yield upload
    finally:
        await api.delete(f"/v1/uploads/{upload['id']}")


async def _until_terminal(api: ApiClient, upload_id: str) -> dict[str, Any]:
    for _ in range(40):
        state: dict[str, Any] = (await api.get(f"/v1/uploads/{upload_id}")).json()
        if state["status"] in {"done", "failed"}:
            return state
        assert state["status"] in {"queued", "processing"}
        assert 0 <= state["progress"] <= 100
        await asyncio.sleep(0.5)
    raise AssertionError("the upload never finished")


def _error(response: httpx2.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body


async def test_upload_lifecycle(api: ApiClient) -> None:
    content = b"symbol,qty\nNVDA,3\nTSM,8\n"
    async with _upload(api, "e2e.csv", content, "text/csv", label="fauxcall-e2e") as upload:
        assert (upload["status"], upload["progress"], upload["label"]) == (
            "queued",
            0,
            "fauxcall-e2e",
        )
        assert upload["size"] == len(content)
        path = f"/v1/uploads/{upload['id']}"
        early = await api.get(f"{path}/content")
        assert (early.status_code, _error(early)["code"]) == (409, "not_ready")
        listed = (await api.get("/v1/uploads")).json()
        assert listed[0]["id"] == upload["id"]  # newest first
        done = await _until_terminal(api, upload["id"])
        assert (done["status"], done["progress"]) == ("done", 100)
        assert "error" not in done
        created = datetime.fromisoformat(done["created_at"])
        assert datetime.fromisoformat(done["completed_at"]) - created == timedelta(seconds=8)
        download = await api.get(f"{path}/content")
        assert download.content == content
        assert download.headers["content-disposition"] == 'attachment; filename="e2e.csv"'
    assert (await api.get(path)).status_code == 404


async def test_corrupt_upload_fails(api: ApiClient) -> None:
    async with _upload(api, "bad.txt", b"header\nCORRUPT row\n", "text/plain") as upload:
        assert upload["label"] is None
        failed = await _until_terminal(api, upload["id"])
        assert (failed["status"], failed["error"]) == ("failed", "corrupt_file")
        refused = await api.get(f"/v1/uploads/{upload['id']}/content")
        assert _error(refused)["message"].endswith(
            "is failed; content is available only when status is done."
        )


async def test_binary_upload_round_trip(api: ApiClient) -> None:
    async with _upload(api, "dot.png", PNG, "image/png", label="") as upload:
        assert upload["label"] is None
        assert (await _until_terminal(api, upload["id"]))["status"] == "done"
        download = await api.get(f"/v1/uploads/{upload['id']}/content")
        assert (download.content, download.headers["content-type"]) == (PNG, "image/png")


async def test_upload_refusals(api: ApiClient) -> None:
    no_file = await api.post("/v1/uploads", json={"file": "x"})
    assert (_error(no_file)["code"], _error(no_file)["param"]) == ("missing_parameter", "file")
    text = await api.post("/v1/uploads", data={"file": "not a file"})
    assert (_error(text)["code"], _error(text)["param"]) == ("invalid_parameter", "file")
    big = await api.post("/v1/uploads", files={"file": ("big.bin", b"\0" * (1024 * 1024 + 7))})
    assert (big.status_code, _error(big)["code"]) == (413, "payload_too_large")
    for content_type, message in [
        ("multipart/form-data", "Missing boundary in multipart."),
        ("multipart/form-data; boundary=e2e", "Invalid multipart data."),
    ]:
        broken = await api.post(
            "/v1/uploads", content=b"--e2e\r\nno part\r\n", headers={"content-type": content_type}
        )
        assert (broken.status_code, _error(broken)["code"]) == (400, "bad_request")
        assert _error(broken)["message"] == message
    missing = await api.delete("/v1/uploads/2b1c0d4e-0000-4000-8000-000000000000")
    assert _error(missing)["code"] == "upload_not_found"
