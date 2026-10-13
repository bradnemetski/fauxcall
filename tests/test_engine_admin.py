"""The admin API and the runtime's remaining paths: custom handlers, start-up, resets, latency."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx2
from fastapi import APIRouter, Request, WebSocket
from httpx2.websockets import ASGIWebSocketTransport
from pydantic import SecretStr
from starlette.responses import Response  # noqa: TC002 - FastAPI reads route annotations

from _engine import API_KEY, harness, j, market_model, with_endpoints
from _mcp import asgi_client, live_server, mcp_app, mcp_model
from fauxcall.config import AuthSettings, FakeSettings, IdentityConfig
from fauxcall.mcpwire import protocol
from fauxcall.model import AuthModel, AuthScheme, CalendarModel, McpTransportModel
from fauxcall.model.realism import LatencyProfile
from fauxcall.runtime import app as app_module
from fauxcall.runtime import create_app, handle, handle_websocket
from fauxcall.storage import write_model

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from fauxcall.runtime import RequestContext


async def test_sessions_clock_operations_and_schema(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client(session="s1") as client:
        await client.post("/v1/watchlists", json={"name": "per session"})
        assert "s1" in (await client.get("/__fauxcall/sessions")).json()["sessions"]
        offset = await client.post("/__fauxcall/clock", json={"op": "offset", "seconds": 3600})
        assert offset.status_code == 200
        real = await client.post("/__fauxcall/clock", json={"op": "real"})
        assert real.json()["mode"] == "real"
        bad = await client.post("/__fauxcall/clock", json={"op": "advance"})
        assert bad.status_code == 400
        assert "seconds" in bad.text
        schema = (await client.get("/__fauxcall/journal/entry-schema")).json()
        assert "properties" in schema


async def test_reset_everything_including_the_journal(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client() as client:
        await client.post("/v1/watchlists", json={"name": "gone soon"})
        assert len(fake.runtime.journal) > 0
        reset = await client.post("/__fauxcall/reset", json={"journal": True})
        assert reset.json() == {"reset": "all"}
        assert len(fake.runtime.journal) == 0  # admin calls are never journaled
        assert (await client.get("/v1/watchlists")).json() == []


async def test_custom_http_and_websocket_handlers(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    router = APIRouter()

    async def guarded(ctx: RequestContext) -> Response:
        if ctx.query("symbol") is None:
            raise ctx.fail("missing_param", param="symbol")
        return ctx.respond({"symbol": ctx.query("symbol")})

    async def echo_socket(websocket: WebSocket, ctx: RequestContext) -> None:
        await websocket.accept()
        await websocket.send_text(f"hello {ctx.identity.name if ctx.identity else 'anonymous'}")
        await websocket.close()

    @router.get("/v1/custom")
    async def custom(request: Request) -> Response:
        return await handle(request, "GET /v1/exchanges", guarded)

    @router.websocket("/v1/custom-ws")
    async def custom_ws(websocket: WebSocket) -> None:
        await handle_websocket(websocket, "GET /v1/ws/quotes", echo_socket)

    app = create_app(
        fake.runtime.settings, routers=[router], runtime=fake.runtime, configure_logs=False
    )
    transport = httpx2.ASGITransport(app=app)
    headers = {"X-API-Key": API_KEY}
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://fake.test", headers=headers
    ) as client:
        assert (await client.get("/v1/custom")).status_code == 400
        assert (await client.get("/v1/custom", params={"symbol": "AAPL"})).json() == {
            "symbol": "AAPL"
        }
    ws_transport = ASGIWebSocketTransport(app=app)
    async with (
        httpx2.AsyncClient(
            transport=ws_transport, base_url="http://fake.test", headers=headers
        ) as client,
        client.websocket("/v1/custom-ws") as ws,
    ):
        assert (await ws.receive_text()).startswith("hello ")


async def test_app_from_settings_with_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    write_model(data / "model.json", market_model())
    calls: list[tuple[Any, ...]] = []

    def configure(*args: Any, **kwargs: Any) -> None:
        calls.append((*args, kwargs))

    monkeypatch.setattr(app_module, "configure_logging", configure)
    settings = FakeSettings(data_dir=data, log_level="INFO")
    app = create_app(settings)
    assert calls == [("INFO", settings.log_format, {"static": {"fake": "market"}})]
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://x"
    ) as client:
        assert (await client.get("/__fauxcall/health")).status_code == 200


def test_calendars_added_after_start_are_found(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    fake.runtime.model.calendars.append(CalendarModel(name="late", zone="Asia/Tokyo"))
    assert str(fake.runtime.calendar("late").zone) == "Asia/Tokyo"


async def test_latency_edges_and_a_disabled_journal(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {"id": "GET /v1/instant", "role": "echo", "latency": {"samples_ms": []}},
    )
    model.endpoint("GET /v1/exchanges").latency = LatencyProfile.from_samples([0.0])
    fake = harness(tmp_path, model=model, latency=True, journal_limit=0)
    async with fake.client() as client:
        assert (await client.get("/v1/exchanges")).status_code == 200
        assert (await client.get("/v1/instant")).status_code == 200
    assert fake.sleeps == []  # nothing left to wait for: the work already took longer
    assert len(fake.runtime.journal) == 0


async def test_mcp_forbidden_callers_get_no_auth_challenge(tmp_path: Path) -> None:
    auth = AuthModel(
        schemes=[AuthScheme(kind="bearer", prefix="Bearer ")],
        missing_template="unauthorized",
        forbidden_template="forbidden",
    )
    users = AuthSettings(
        users=[IdentityConfig(name="mallory", status="denied", bearer_token=SecretStr("m-token"))]
    )
    app, _ = mcp_app(tmp_path, mcp_model(auth=auth), auth=users)
    async with asgi_client(app, authorization="Bearer m-token") as http:
        denied = await http.post("/mcp", json=protocol.request(1, "ping"))
    assert denied.status_code == 403
    assert "www-authenticate" not in denied.headers
    assert j(denied.json()) == {"error": "forbidden"}


async def test_idle_get_stream_sends_keep_alives(tmp_path: Path) -> None:
    # the real server's text and interval (wall clock: never noted as a pause)
    transport = McpTransportModel(
        get_stream="sse", keep_alive=": keepalive", keep_alive_seconds=0.01
    )
    app, runtime = mcp_app(tmp_path, mcp_model(transport=transport))
    async with live_server(app) as base, httpx2.AsyncClient(base_url=base) as http:
        async with http.stream("GET", "/mcp", headers={"accept": "text/event-stream"}) as stream:
            first = await anext(stream.aiter_bytes())
        assert first == b": keepalive\n\n"
        assert not runtime.pauses
