"""Every recorded request, replayed against the fake, gets the recorded status and body shape.

Stateful endpoints (writes, jobs, downloads) are excluded here because a fresh fake does not hold
the resources created while recording; cover them with scenarios in ``tests/e2e``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.support import RecordedCase, recorded_cases, replay_against

from fauxcall_petstore.app import build_app
from fauxcall_petstore.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

WRITES = frozenset(
    {
        "POST /api/v3/pet",
        "PUT /api/v3/pet",
        "POST /api/v3/pet/{petId}",
        "DELETE /api/v3/pet/{petId}",
        "POST /api/v3/pet/{petId}/uploadImage",
        "POST /api/v3/store/order",
        "DELETE /api/v3/store/order/{orderId}",
        "POST /api/v3/user",
        "POST /api/v3/user/createWithList",
        "PUT /api/v3/user/{username}",
        "DELETE /api/v3/user/{username}",
    }
)
"""Custom write handlers act on records created while recording: covered by test_handlers.py."""

CASES = recorded_cases(Settings().data_dir, exclude_endpoints=WRITES)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Built once (a large world takes a moment to load); every replay starts from a reset."""
    return build_app(Settings(fast=True, log_level="WARNING"))


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_recorded_request(app: FastAPI, case: RecordedCase) -> None:
    runtime_of_app(app).reset(journal=True)
    outcome = await replay_against(app, case)
    assert outcome.ok, outcome.explain()
