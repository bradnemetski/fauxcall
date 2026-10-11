"""Every recorded write (POST/PUT/DELETE) replayed against the fake gives the recorded answer.

Stricter than ``test_recorded.py``: status, content type, version header and the parsed body
(System.Text.Json messages with their byte positions, echoed models) must be equal; where the
recording kept the raw response text, the bytes must be equal too (escaping, key order). Only
the random ``traceId`` and the host in ``UnsupportedApiVersion`` messages differ by design.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from fauxcall.testing.support import settings_for_tests

from fauxcall_fakerestapi.app import build_app
from fauxcall_fakerestapi.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

REAL_HOST = "https://fakerestapi.azurewebsites.net"
FAKE_HOST = "http://fake"
RECORDINGS = Path(Settings().data_dir) / "recordings"
WRITES = sorted(
    path for prefix in ("post-", "put-", "delete-") for path in RECORDINGS.glob(f"{prefix}*/*.json")
)


def _request_bytes(body: dict[str, Any]) -> bytes:
    """The bytes the recorder sent (compact JSON, or the text as is)."""
    if body.get("kind") == "json":
        for ascii_only in (False, True):
            raw = json.dumps(body["json"], separators=(",", ":"), ensure_ascii=ascii_only).encode()
            if len(raw) == body.get("size"):
                return raw
        return raw
    return str(body.get("text") or "").encode()


def _comparable(body: Any) -> Any:
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k != "traceId"}
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            error["message"] = error["message"].replace(REAL_HOST, FAKE_HOST)
    return body


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


EXCHANGES = [json.loads(path.read_text()) for path in WRITES]


@pytest.mark.parametrize("exchange", EXCHANGES, ids=[p.stem for p in WRITES])
async def test_recorded_write(app: FastAPI, exchange: dict[str, Any]) -> None:
    request, expected = exchange["request"], exchange["response"]
    headers = {
        name: value
        for name, value in request["headers"]
        if name.lower() in {"content-type", "accept"}
    }
    body = request.get("body") or {}
    content = _request_bytes(body) if body else b""
    if body and "content-type" not in headers and body.get("content_type"):
        headers["content-type"] = body["content_type"]
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=FAKE_HOST
    ) as client:
        response = await client.request(
            request["method"], request["path"], content=content, headers=headers
        )
    assert response.status_code == expected["status"]
    recorded_headers = {k.lower(): v for k, v in expected["headers"]}
    assert response.headers.get("content-type") == recorded_headers.get("content-type")
    assert response.headers.get("api-supported-versions") == recorded_headers.get(
        "api-supported-versions"
    )
    recorded_body = (expected.get("body") or {}).get("json")
    if recorded_body is None:
        assert response.content == b""
    else:
        assert _comparable(response.json()) == _comparable(recorded_body)
        recorded_text = (expected.get("body") or {}).get("text")
        if recorded_text is not None and "traceId" not in recorded_text:
            assert response.text == recorded_text
