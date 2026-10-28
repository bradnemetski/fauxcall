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

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

INTERACTIVE: frozenset[str] = frozenset(
    {
        # these ask the client mid-call (sampling, elicitation, roots): a bare replay can't answer;
        # tests/unit/test_interactive.py drives them with a client that does
        "MCP tools/call:trigger-sampling-request",
        "MCP tools/call:trigger-elicitation-request",
        "MCP tools/call:trigger-url-elicitation",
        "MCP tools/call:get-roots-list",
        "MCP tools/call:trigger-sampling-request-async",
        "MCP tools/call:trigger-elicitation-request-async",
        # a session resource exists only after gzip-file-as-resource ran in the same session
        "MCP resources/read:demo://resource/session/hello.txt.gz",
    }
)
CASES = recorded_cases(Settings().data_dir, exclude_endpoints=INTERACTIVE)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Built once (a large world takes a moment to load); every replay starts from a reset."""
    return build_app(settings_for_tests(Settings))


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_recorded_request(app: FastAPI, case: RecordedCase) -> None:
    runtime_of_app(app).reset(journal=True)
    outcome = await replay_against(app, case)
    assert outcome.ok, outcome.explain()
