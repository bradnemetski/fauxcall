"""Every recorded request, replayed against the fake, gets the recorded status and body shape.

Stateful endpoints (writes, jobs, downloads) are excluded here because a fresh fake does not hold
the resources created while recording; cover them with scenarios in ``tests/e2e``.

Recordings redact every ``X-API-Key`` value, so the auth probes are replayed with the key their
plan sent (the API's documented demo keys, none, or an invalid one) instead of the harness's
placeholder: that is what distinguishes a revoked key (403) from a missing one (401).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final, cast

import pytest
from fauxcall.testing.support import RecordedCase, recorded_cases, replay_against

from fauxcall_refapi.app import build_app
from fauxcall_refapi.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping
    from pathlib import Path

    from fastapi import FastAPI

    Scope = MutableMapping[str, Any]
    Asgi = Callable[[Scope, Any, Any], Awaitable[None]]

DATA_DIR = Settings().data_dir
STATEFUL: Final = frozenset(
    {
        "GET /v1/watchlists/{id}",
        "PUT /v1/watchlists/{id}",
        "PATCH /v1/watchlists/{id}",
        "GET /v1/uploads/{id}",
        "GET /v1/uploads/{id}/content",
    }
)
"""Endpoints whose successful (or not-ready) answers need a record created while recording."""


def stateless(case: RecordedCase) -> bool:
    """Whether a fresh fake can reproduce ``case`` (errors about unknown ids it can)."""
    response = case.exchange.response
    status = response.status if response is not None else 0
    return case.exchange.endpoint not in STATEFUL or status not in {200, 409}


CASES = [case for case in recorded_cases(DATA_DIR) if stateless(case)]
INVALID_KEY: Final = "fauxcall-invalid-key"
_DEFAULT: Final = object()


def plan_keys(data_dir: Path) -> dict[str, str | None]:
    """Plan call id -> the X-API-Key it sent (``None``: no key) for calls not using the default."""
    keys: dict[str, str | None] = {}
    for path in sorted((data_dir / "plans").glob("*.json")):
        for call in json.loads(path.read_text(encoding="utf-8"))["calls"]:
            request = call["request"]
            headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
            if "x-api-key" in headers:
                keys[call["id"]] = headers["x-api-key"]
            elif request.get("auth") == "none":
                keys[call["id"]] = None
            elif request.get("auth") == "invalid":
                keys[call["id"]] = INVALID_KEY
    return keys


KEYS = plan_keys(DATA_DIR)


def with_api_key(app: FastAPI, key: str | None) -> FastAPI:
    """``app`` seeing ``key`` as the request's X-API-Key (or none at all)."""
    inner = cast("Asgi", app)

    async def wrapped(scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"x-api-key"]
            if key is not None:
                headers.append((b"x-api-key", key.encode()))
            scope = {**scope, "headers": headers}
        await inner(scope, receive, send)

    return cast("FastAPI", wrapped)


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_recorded_request(case: RecordedCase) -> None:
    app = build_app(Settings(fast=True, log_level="WARNING"))
    key = KEYS.get(case.exchange.plan_call or "", _DEFAULT)
    if key is not _DEFAULT:
        app = with_api_key(app, cast("str | None", key))
    outcome = await replay_against(app, case)
    assert outcome.ok, outcome.explain()


def test_plan_keys_cover_the_auth_probes() -> None:
    assert KEYS["ex-list-revoked"] == "revoked-key"
    assert KEYS["ex-list-readonly"] == "readonly-key"
    assert KEYS["ex-list-noauth"] is None
    assert KEYS["ex-list-badkey"] == INVALID_KEY
