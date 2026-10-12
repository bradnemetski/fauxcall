from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import uvicorn
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything import __main__ as entrypoint
from fauxcall_everything.app import build_app
from fauxcall_everything.handlers import TOOL_HANDLERS
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    import pytest
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue


def _client() -> httpx2.AsyncClient:
    app = build_app(settings_for_tests(Settings))
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://fake.test")


async def test_admin_health() -> None:
    async with _client() as client:
        response = await client.get("/__fauxcall/health")
    assert response.status_code == 200
    assert response.json()["fake"] == "everything"


async def test_unknown_route_uses_the_real_not_found() -> None:
    async with _client() as client:
        response = await client.get("/__definitely_not_a_route__")
    assert response.status_code in {404, 405}


def test_main_serves_with_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda _app, **kwargs: calls.append(kwargs))
    entrypoint.main()
    assert calls[0]["port"] == Settings().port


def test_custom_tool_handlers_are_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def tool(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
        del ctx
        return {"content": [], "isError": False, "echo": arguments}

    monkeypatch.setitem(TOOL_HANDLERS, "__probe__", tool)
    fake = runtime_of_app(build_app(settings_for_tests(Settings))).mcp
    assert fake is not None
    assert fake.tool_handlers["__probe__"] is tool
