"""The fake's ASGI app."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fauxcall.runtime import create_app

from fauxcall_refapi.handlers import fallback_router
from fauxcall_refapi.routes import router
from fauxcall_refapi.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (used by ``__main__`` and the tests).

    ``fallback_router`` comes last: it only answers requests no real endpoint matches.
    """
    return create_app(settings or Settings(), routers=[router, fallback_router])
