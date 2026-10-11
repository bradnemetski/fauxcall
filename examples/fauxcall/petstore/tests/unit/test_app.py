from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import uvicorn

from fauxcall_petstore import __main__ as entrypoint
from fauxcall_petstore.app import build_app
from fauxcall_petstore.settings import Settings

if TYPE_CHECKING:
    import pytest


def _client() -> httpx2.AsyncClient:
    app = build_app(Settings(fast=True, log_level="WARNING"))
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://fake.test")


async def test_admin_health() -> None:
    async with _client() as client:
        response = await client.get("/__fauxcall/health")
    assert response.status_code == 200
    assert response.json()["fake"] == "petstore"


async def test_unknown_route_uses_the_real_not_found() -> None:
    async with _client() as client:
        response = await client.get("/__definitely_not_a_route__")
    assert response.status_code in {404, 405}


def test_main_serves_with_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda _app, **kwargs: calls.append(kwargs))
    entrypoint.main()
    assert calls[0]["port"] == Settings().port
