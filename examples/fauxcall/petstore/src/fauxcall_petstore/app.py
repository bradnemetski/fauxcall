"""The fake's ASGI app."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fauxcall.runtime import create_app

from fauxcall_petstore import server
from fauxcall_petstore.routes import router
from fauxcall_petstore.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (used by ``__main__`` and the tests)."""
    app = create_app(settings or Settings(), routers=[router])
    server.install(app)
    return app
