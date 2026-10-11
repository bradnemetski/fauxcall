"""Liveness (no auth, no rate limit, no latency)."""

from __future__ import annotations

from fastapi import APIRouter

from fauxcall_reference_api.schemas import HealthOut

router = APIRouter()


@router.get("/healthz", tags=["health"], summary="Liveness")
async def healthz() -> HealthOut:
    """Always ``{"status": "ok"}``."""
    return HealthOut(status="ok")
