"""The application factory: REST + SSE + WebSocket + MCP in one ASGI app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

from mcp.server.transport_security import TransportSecuritySettings

from fauxcall_reference_api import API_TITLE, API_VERSION
from fauxcall_reference_api.clock import SystemClock, real_sleep
from fauxcall_reference_api.context import AppContext
from fauxcall_reference_api.errors import install_error_handlers
from fauxcall_reference_api.gateway import Gateway
from fauxcall_reference_api.mcp_server import build_mcp_server
from fauxcall_reference_api.openapi import DESCRIPTION, TAGS, ReferenceAPI
from fauxcall_reference_api.routes import health, reference, streaming, uploads, watchlists
from fauxcall_reference_api.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

    from fauxcall_reference_api.clock import Clock, Sleep

MCP_PATH: Final = "/mcp"
ROUTERS: Final = (
    health.router,
    reference.router,
    watchlists.router,
    uploads.router,
    streaming.router,
    streaming.socket_router,
)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
) -> FastAPI:
    """Build the reference API.

    Args:
        settings: Configuration; defaults to :class:`Settings` read from ``REFAPI_*`` variables.
        clock: Time source; defaults to the system clock.
        sleep: Awaitable sleep used for latency and stream pacing; defaults to real sleeping.
    """
    context = AppContext(
        settings=settings if settings is not None else Settings(),
        clock=clock if clock is not None else SystemClock(),
        sleep=sleep if sleep is not None else real_sleep,
    )
    mcp = build_mcp_server(context)
    # DNS-rebinding protection is off: this is a local test server reached by arbitrary host names
    # (127.0.0.1, localhost, container names, test clients).
    mcp_app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = ReferenceAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        redoc_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.context = context
    app.state.mcp = mcp
    app.state.api_routes = tuple(route for router in ROUTERS for route in router.routes)
    for router in ROUTERS:
        app.include_router(router)
    # Add the MCP endpoint as an exact route (a Mount would redirect /mcp to /mcp/).
    app.router.routes.extend(mcp_app.routes)
    install_error_handlers(app)
    app.add_middleware(Gateway, context=context)
    return app
