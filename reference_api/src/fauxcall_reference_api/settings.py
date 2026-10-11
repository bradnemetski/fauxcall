"""Runtime configuration (pydantic-settings). Every field can be set as ``REFAPI_<NAME>``.

Complex values are JSON, e.g. ``REFAPI_API_KEYS='{"demo-key": "full", "ci-key": "readonly"}'``.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

type KeyAccess = Literal["full", "readonly", "revoked"]
"""``full``: any method; ``readonly``: GET/HEAD/OPTIONS only; ``revoked``: always 403."""

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_API_KEYS: Final[dict[str, KeyAccess]] = {
    "demo-key": "full",
    "readonly-key": "readonly",
    "revoked-key": "revoked",
}


class Settings(BaseSettings):
    """Settings for the reference API server."""

    model_config = SettingsConfigDict(env_prefix="REFAPI_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=9100, ge=0, le=65535)
    latency: bool = Field(
        default=True, description="Sleep 15-60 ms (deterministic per method+path) on /v1 requests."
    )
    log_level: LogLevel = "INFO"
    api_keys: dict[str, KeyAccess] = Field(default_factory=lambda: dict(DEFAULT_API_KEYS))
    rate_limit_requests: int = Field(default=30, ge=1, description="Requests per window per key.")
    rate_limit_window_seconds: int = Field(default=10, ge=1)
