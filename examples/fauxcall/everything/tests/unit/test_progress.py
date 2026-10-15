"""Progress notifications: every recorded call that asked for them, replayed byte for byte.

The SDK sends ``{"method":"notifications/progress","params":{progress,total,progressToken},
"jsonrpc":"2.0"}`` on the call's own stream before its answer (000220-000229, 000392); the fake
must send the same events, in the same order and the same spelling (``"total":2.5``), before the
same answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from fauxcall.recording.store import RecordingStore
from fauxcall.testing.support import settings_for_tests
from mcp_session import connect

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fauxcall.model.recording import Exchange


def _asked_for_progress() -> list[Exchange]:
    found = []
    for _, exchange in RecordingStore(Settings().data_dir / "recordings").iter_exchanges():
        sent = exchange.request.body.parsed()
        params = sent.get("params") if isinstance(sent, dict) else None
        meta = params.get("_meta") if isinstance(params, dict) else None
        if isinstance(meta, dict) and "progressToken" in meta and exchange.stream:
            found.append(exchange)
    return found


PROGRESS: Final = _asked_for_progress()


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


def test_progress_was_recorded() -> None:
    assert len(PROGRESS) >= 5


@pytest.mark.parametrize("exchange", PROGRESS, ids=[e.id for e in PROGRESS])
async def test_progress_events_as_recorded(app: FastAPI, exchange: Exchange) -> None:
    sent = exchange.request.body.parsed()
    assert isinstance(sent, dict)
    async with connect(app) as wire:
        call = await wire.call(str(sent["method"]), sent["params"])
    recorded = [
        e.data.replace(f'"id":{sent["id"]}', '"id":<id>') for e in exchange.stream or [] if e.data
    ]
    assert isinstance(call.sent, dict)
    fake = [
        data.replace(f'"id":{call.sent["id"]}', '"id":<id>')
        for _, _, data, _ in call.sse_events
        if data
    ]
    assert fake == recorded
