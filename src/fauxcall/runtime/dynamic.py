"""Serve a model without generated code: one route per endpoint, built at startup.

Used by ``fauxcall serve`` before a fake is scaffolded, and by tests of the engine itself. Generated
fakes use explicit routes instead (same ordering rules, same entry points).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter
from starlette.requests import Request  # noqa: TC002 - FastAPI resolves route annotations at runtime
from starlette.responses import Response  # noqa: TC002
from starlette.websockets import WebSocket  # noqa: TC002

from fauxcall.runtime.app import handle, handle_websocket
from fauxcall.runtime.routing import route_identifier

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fauxcall.model.api import ApiModel
    from fauxcall.model.endpoints import EndpointModel

_PARAM: Final = re.compile(r"\{([^{}/]+)\}")


def specificity(endpoint: EndpointModel) -> tuple[int, int, str]:
    """Sort key: more literal characters first, then fewer parameters."""
    return (-len(_PARAM.sub("", endpoint.path)), len(_PARAM.findall(endpoint.path)), endpoint.path)


def routable(path: str) -> str:
    """Path with parameter names Starlette accepts."""
    return _PARAM.sub(lambda m: "{" + route_identifier(m.group(1)) + "}", path)


def _http_endpoint(endpoint_id: str) -> Callable[[Request], Awaitable[Response]]:
    async def serve(request: Request) -> Response:
        return await handle(request, endpoint_id)

    return serve


def _ws_endpoint(endpoint_id: str) -> Callable[[WebSocket], Awaitable[None]]:
    async def serve(websocket: WebSocket) -> None:
        await handle_websocket(websocket, endpoint_id)

    return serve


def dynamic_router(model: ApiModel) -> APIRouter:
    """Routes for every REST endpoint of ``model``, most specific first."""
    router = APIRouter()
    for endpoint in sorted(model.endpoints, key=specificity):
        path = routable(endpoint.path)
        if endpoint.role == "websocket":
            router.add_api_websocket_route(path, _ws_endpoint(endpoint.id))
        else:
            router.add_api_route(
                path,
                _http_endpoint(endpoint.id),
                methods=[endpoint.method.value],
                include_in_schema=False,
            )
    return router
