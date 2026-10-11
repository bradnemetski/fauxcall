"""Latency and rate limits measured on (or documented for) the real API."""

from __future__ import annotations

import statistics
from typing import Literal

from pydantic import Field, model_validator

from fauxcall.model.common import Model

MAX_LATENCY_SAMPLES = 200


class LatencyProfile(Model):
    """Observed response times. The fake resamples them (bootstrap), seeded per request."""

    samples_ms: list[float] = Field(default_factory=list, max_length=MAX_LATENCY_SAMPLES)
    p50_ms: float = Field(default=0.0, ge=0)
    p90_ms: float = Field(default=0.0, ge=0)
    p99_ms: float = Field(default=0.0, ge=0)

    @classmethod
    def from_samples(cls, samples: list[float]) -> LatencyProfile:
        """Build a profile, keeping at most :data:`MAX_LATENCY_SAMPLES` evenly spaced samples."""
        clean = sorted(s for s in samples if s >= 0)
        if not clean:
            return cls()
        if len(clean) > MAX_LATENCY_SAMPLES:
            step = len(clean) / MAX_LATENCY_SAMPLES
            clean = [clean[int(i * step)] for i in range(MAX_LATENCY_SAMPLES)]

        def pct(p: float) -> float:
            if len(clean) == 1:
                return clean[0]
            return statistics.quantiles(clean, n=100, method="inclusive")[int(p) - 1]

        return cls(samples_ms=clean, p50_ms=pct(50), p90_ms=pct(90), p99_ms=pct(99))


RateLimitKey = Literal["global", "ip", "credential", "session"]


class RateLimitModel(Model):
    """A rate limit the real API enforces.

    ``headers`` maps response header names to templates using ``{{limit}}``, ``{{remaining}}``,
    ``{{reset_epoch}}``, ``{{reset_seconds}}`` and ``{{window_seconds}}``.
    """

    name: str
    limit: int = Field(ge=1)
    window_seconds: float = Field(gt=0)
    key: RateLimitKey = "credential"
    algorithm: Literal["fixed_window", "sliding_window", "token_bucket"] = "fixed_window"
    endpoints: list[str] = Field(default_factory=lambda: ["*"], description="Endpoint id globs.")
    headers: dict[str, str] = Field(default_factory=dict)
    headers_on_success: bool = Field(
        default=True, description="Whether rate-limit headers appear on non-429 responses."
    )
    error_template: str | None = Field(default=None, description="Template for the 429 response.")
    retry_after: bool = True
    source: Literal["documented", "headers", "probed", "assumed"] = "documented"

    @model_validator(mode="after")
    def _sane(self) -> RateLimitModel:
        if not self.endpoints:
            raise ValueError("a rate limit applies to at least one endpoint glob")
        return self
