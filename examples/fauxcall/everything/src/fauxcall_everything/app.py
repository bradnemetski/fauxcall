"""The fake's ASGI app."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fauxcall.runtime import create_app, register_method, register_tool
from fauxcall.runtime.app import runtime_of_app

from fauxcall_everything.front import ExpressFront
from fauxcall_everything.handlers import METHOD_HANDLERS, TOOL_HANDLERS
from fauxcall_everything.routes import router
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (used by ``__main__`` and the tests).

    The real server's Express front (CORS, its 404 page, the transport's HTTP refusals) stands
    before FauxCall's MCP runtime: see :mod:`fauxcall_everything.front`.
    """
    settings = settings or Settings()
    app = create_app(settings, routers=[router])
    for name, handler in TOOL_HANDLERS.items():
        register_tool(app, name, handler)
    for method, method_handler in METHOD_HANDLERS.items():
        register_method(app, method, method_handler)
    app.add_middleware(
        ExpressFront, admin_prefix=settings.admin_prefix, runtime=runtime_of_app(app)
    )
    return app
