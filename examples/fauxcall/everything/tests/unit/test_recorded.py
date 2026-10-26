"""Every recorded request, replayed against the fake, gets the recorded status and body shape.

Stateful endpoints (writes, jobs, downloads) are excluded here because a fresh fake does not hold
the resources created while recording; cover them with scenarios in ``tests/e2e``.

Tools the server lists only to clients declaring sampling, elicitation or roots are excluded too:
a replay opens its session without capabilities and can't answer the server's requests, so the
server (real and fake alike) would refuse the tool as unknown. ``test_client_tools.py`` replays
them with the recorded capabilities and client answers.
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

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

CAPABILITY_TOOLS = frozenset(
    f"MCP tools/call:{tool}"
    for tool in (
        "get-roots-list",
        "trigger-elicitation-request",
        "trigger-url-elicitation",
        "trigger-sampling-request",
        "trigger-sampling-request-async",
        "trigger-elicitation-request-async",
    )
)
CASES = recorded_cases(Settings().data_dir, exclude_endpoints=CAPABILITY_TOOLS)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Built once (a large world takes a moment to load); every replay starts from a reset."""
    return build_app(settings_for_tests(Settings))


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_recorded_request(app: FastAPI, case: RecordedCase) -> None:
    runtime_of_app(app).reset(journal=True)
    outcome = await replay_against(app, case)
    assert outcome.ok, outcome.explain()
