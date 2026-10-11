"""The fake's ASGI app."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fauxcall.runtime import create_app

from fauxcall_frankfurter.middleware import RealHeaders
from fauxcall_frankfurter.routes import router
from fauxcall_frankfurter.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (used by ``__main__`` and the tests)."""
    settings = settings or Settings()
    app = create_app(settings, routers=[router])
    app.add_middleware(RealHeaders, admin_prefix=settings.admin_prefix)
    return app
