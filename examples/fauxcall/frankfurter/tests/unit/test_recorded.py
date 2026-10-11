"""Every recorded request, replayed against the fake, gets the recorded status and body shape.

Stateful endpoints (writes, jobs, downloads) are excluded here because a fresh fake does not hold
the resources created while recording; cover them with scenarios in ``tests/e2e``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fauxcall.testing.support import RecordedCase, recorded_cases, replay_against

from fauxcall_frankfurter.app import build_app
from fauxcall_frankfurter.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

CASES = recorded_cases(Settings().data_dir)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """One fake for every case: all Frankfurter endpoints are read-only and stateless."""
    return build_app(Settings(fast=True, log_level="WARNING"))


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_recorded_request(app: FastAPI, case: RecordedCase) -> None:
    outcome = await replay_against(app, case)
    assert outcome.ok, outcome.explain()
