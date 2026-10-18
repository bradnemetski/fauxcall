"""Every recorded request, replayed against the fake, gets the recorded status and body shape.

Stateful endpoints (writes, jobs, downloads) are excluded here because a fresh fake does not hold
the resources created while recording; cover them with scenarios in ``tests/e2e``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.support import (
    RecordedCase,
    recorded_cases,
    replay_against,
    settings_for_tests,
)

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

CAPABLE_CLIENTS_ONLY: Final = frozenset(
    {
        "MCP tools/call:get-roots-list",
        "MCP tools/call:trigger-sampling-request",
        "MCP tools/call:trigger-sampling-request-async",
        "MCP tools/call:trigger-elicitation-request",
        "MCP tools/call:trigger-elicitation-request-async",
        "MCP tools/call:trigger-url-elicitation",
    }
)
"""Tools the server lists only to clients declaring sampling, elicitation or roots.

A replay here opens a session without capabilities, where the real server (and the fake) says
"Tool not found": their recordings, made as capable clients, are replayed in ``test_plans.py``
with the recorded capabilities and client answers. The calls recorded without them stay here.
"""


def _replayable(case: RecordedCase) -> bool:
    return case.exchange.endpoint not in CAPABLE_CLIENTS_ONLY or (
        case.exchange.plan_call or ""
    ).startswith(("no-cap-", "roots-unprompted"))


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
