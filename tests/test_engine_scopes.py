"""Authorization by scope: checked after rate limiting, so refused calls still count."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr

from _engine import harness, j, market_model
from fauxcall.config import AuthSettings, IdentityConfig
from fauxcall.model import RateLimitModel

if TYPE_CHECKING:
    from pathlib import Path

    from _engine import Harness

READ_ONLY = "readonly-key"
WRITER = "writer-key"


def _fake(tmp_path: Path) -> Harness:
    model = market_model()
    assert model.auth is not None
    model.auth.required_scopes = {"POST *": ["write"], "DELETE *": ["write"], "GET *": ["read"]}
    model.rate_limits = [
        RateLimitModel(
            name="per_key",
            limit=100,
            window_seconds=10,
            key="credential",
            endpoints=["*"],
            headers={"X-RateLimit-Remaining": "{{remaining}}"},
        )
    ]
    users = AuthSettings(
        users=[
            IdentityConfig(name="reader", api_key=SecretStr(READ_ONLY), scopes=["read"]),
            IdentityConfig(name="writer", api_key=SecretStr(WRITER), scopes=["read", "write"]),
        ]
    )
    return harness(tmp_path, model=model, auth=users)


async def test_read_only_callers_are_refused_writes_after_counting(tmp_path: Path) -> None:
    fake = _fake(tmp_path)
    async with fake.client(key=READ_ONLY) as reader:
        assert (await reader.get("/v1/exchanges")).status_code == 200
        refused = await reader.post("/v1/watchlists", json={"name": "nope"})
        assert refused.status_code == 403
        assert j(refused.json())["error"]["code"] == "forbidden"  # the forbidden template
        assert refused.headers["X-RateLimit-Remaining"] == "98"  # counted, like the real API
    async with fake.client(key=WRITER) as writer:
        assert (await writer.post("/v1/watchlists", json={"name": "ok"})).status_code == 201
    async with fake.client(key="any-other-key") as anyone:  # no scopes listed: unrestricted
        assert (await anyone.post("/v1/watchlists", json={"name": "fine"})).status_code == 201


async def test_endpoints_without_required_scopes_are_open_to_scoped_callers(
    tmp_path: Path,
) -> None:
    fake = _fake(tmp_path)
    async with fake.client(key=READ_ONLY) as reader:
        put = await reader.put("/v1/watchlists/1", json={"name": "x", "symbols": []})
        assert put.status_code != 403  # PUT requires nothing here
