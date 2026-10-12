"""Every generated route reaches the engine (the other tests check what it answers).

A fault rule makes the engine answer every HTTP request at once with status 599, so that status
proves the request was routed to one of this fake's endpoints (and streams cannot stall the test).
"""

from __future__ import annotations

import contextlib
import re

import httpx2
from fastapi.routing import APIRoute, APIWebSocketRoute
from fauxcall.config import FaultRule
from fauxcall.testing.support import settings_for_tests
from httpx2.websockets import ASGIWebSocketTransport, WebSocketDisconnect

from fauxcall_everything.app import build_app
from fauxcall_everything.routes import router
from fauxcall_everything.settings import Settings

_PARAM = re.compile(r"\{[^{}/]+\}")
_INJECTED = 599


async def test_every_route_reaches_the_engine() -> None:
    settings = settings_for_tests(Settings, faults=[FaultRule(status=_INJECTED)])
    async with (
        ASGIWebSocketTransport(app=build_app(settings)) as transport,
        httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client,
    ):
        for route in router.routes:
            if isinstance(route, APIRoute):
                path = _PARAM.sub("1", route.path)
                for method in sorted(route.methods or ()):
                    response = await client.request(method, path)
                    assert response.status_code == _INJECTED, (method, path)
            elif isinstance(route, APIWebSocketRoute):
                with contextlib.suppress(WebSocketDisconnect):
                    async with client.websocket(_PARAM.sub("1", route.path)):
                        pass
