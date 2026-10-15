"""Every recorded request, replayed against the fake, gets the recorded status and body shape.

Stateful endpoints (writes, jobs, downloads) are excluded here because a fresh fake does not hold
the resources created while recording; cover them with scenarios in ``tests/e2e``.

Tools the server lists only to clients declaring a capability (sampling, elicitation, roots) are
excluded too: a replay opens a session without capabilities, where the real server would not
have them either. ``test_client_requests.py`` replays them in sessions that declare them.

A recording FauxCall's runtime answers before the fake's handlers can is expected to differ
(strictly: the day it no longer does, the test says so). The README lists these differences.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fauxcall.model.api import ApiModel
from fauxcall.runtime.app import runtime_of_app
from fauxcall.storage import read_model
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


def _gated_tools() -> frozenset[str]:
    """Endpoint ids of the tools only capable clients see."""
    model = read_model(Settings().data_dir / "model.json", ApiModel)
    tools = model.mcp.tools if model.mcp is not None else []
    return frozenset(f"MCP tools/call:{tool.name}" for tool in tools if any(tool.requires))


CASES = recorded_cases(Settings().data_dir, exclude_endpoints=_gated_tools())
RUNTIME_GAPS = {
    "000455": "an unknown tool is refused before its arguments are checked (real: zod first)",
}


def _case(case: RecordedCase) -> object:
    gap = RUNTIME_GAPS.get(case.exchange.id)
    if gap is None:
        return pytest.param(case, id=case.id)
    return pytest.param(case, id=case.id, marks=pytest.mark.xfail(strict=True, reason=gap))


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Built once (a large world takes a moment to load); every replay starts from a reset."""
    return build_app(settings_for_tests(Settings))


@pytest.mark.parametrize("case", [_case(case) for case in CASES])
async def test_recorded_request(app: FastAPI, case: RecordedCase) -> None:
    runtime_of_app(app).reset(journal=True)
    outcome = await replay_against(app, case)
    assert outcome.ok, outcome.explain()
