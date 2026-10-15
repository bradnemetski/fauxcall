from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import uvicorn
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.support import settings_for_tests
from fauxcall.world import check_world

from fauxcall_refapi import __main__ as entrypoint
from fauxcall_refapi.app import build_app
from fauxcall_refapi.settings import Settings

if TYPE_CHECKING:
    import pytest


def _client() -> httpx2.AsyncClient:
    app = build_app(settings_for_tests(Settings))
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://fake.test")


async def test_admin_health() -> None:
    async with _client() as client:
        response = await client.get("/__fauxcall/health")
    assert response.status_code == 200
    assert response.json()["fake"] == "refapi"


async def test_unknown_route_uses_the_real_not_found() -> None:
    async with _client() as client:
        response = await client.get("/__definitely_not_a_route__")
    assert response.status_code in {404, 405}


async def test_wrong_method_outside_v1_lists_allow() -> None:
    async with _client() as client:
        health = await client.post("/healthz")
        spec = await client.delete("/openapi.json")
        unknown = await client.post("/nope")
        admin = await client.get("/__fauxcall/health")
    assert (health.status_code, health.headers["allow"]) == (405, "GET")
    assert (spec.status_code, spec.headers["allow"]) == (405, "")
    assert unknown.status_code == 404
    assert "allow" not in unknown.headers
    assert "allow" not in admin.headers


def test_the_world_is_consistent() -> None:
    """Every reference resolves, every target has its minimum of referrers, invariants hold."""
    runtime = runtime_of_app(build_app(settings_for_tests(Settings)))
    assert check_world(runtime.model, runtime.world.session()) == []


def test_main_serves_with_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda _app, **kwargs: calls.append(kwargs))
    entrypoint.main()
    assert calls[0]["port"] == Settings().port
