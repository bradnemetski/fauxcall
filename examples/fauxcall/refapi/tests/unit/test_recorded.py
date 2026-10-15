"""Every recorded request, replayed against the fake, gets the recorded status and body shape.

Stateful endpoints (writes, jobs, downloads) are excluded here because a fresh fake does not hold
the resources created while recording; cover them with scenarios in ``tests/e2e``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.support import (
    RecordedCase,
    recorded_cases,
    replay_against,
    settings_for_tests,
)

from fauxcall_refapi.app import build_app
from fauxcall_refapi.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

ITEM_ENDPOINTS = frozenset(
    {
        "GET /v1/watchlists/{id}",
        "PUT /v1/watchlists/{id}",
        "PATCH /v1/watchlists/{id}",
        "DELETE /v1/watchlists/{id}",
        "GET /v1/uploads/{id}",
        "DELETE /v1/uploads/{id}",
        "GET /v1/uploads/{id}/content",
    }
)
"""Endpoints (custom handlers) on watchlists and uploads, which only exist once created."""

CONFLICT = 409


def _replayable(case: RecordedCase) -> bool:
    """Whether a fresh fake can answer this recording as recorded.

    Answers about a watchlist or upload created while recording (2xx, 409 not_ready) need that
    resource, and so do their unknown-symbol refusals (symbols are checked once the watchlist
    exists, 000802); errors for unknown or malformed ids replay fine. Auth probes that sent one
    of the documented keys (readonly-key, revoked-key) were redacted on recording and are
    replayed without credentials, so only their 401s replay. All are covered by e2e scenarios.
    """
    exchange = case.exchange
    status = exchange.response.status if exchange.response is not None else 0
    body = exchange.response.body.json_value if exchange.response is not None else None
    error = body.get("error") if isinstance(body, dict) else None
    unknown_symbol = isinstance(error, dict) and error.get("code") == "unknown_symbol"
    if exchange.endpoint in ITEM_ENDPOINTS and (
        status < 300 or status == CONFLICT or unknown_symbol
    ):
        return False
    return not (exchange.purpose == "auth" and status != 401)


CASES = [case for case in recorded_cases(Settings().data_dir) if _replayable(case)]


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Built once (a large world takes a moment to load); every replay starts from a reset."""
    return build_app(settings_for_tests(Settings))


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_recorded_request(app: FastAPI, case: RecordedCase) -> None:
    runtime_of_app(app).reset(journal=True)
    outcome = await replay_against(app, case)
    assert outcome.ok, outcome.explain()
