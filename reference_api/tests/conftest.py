from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from fauxcall_reference_api.app import create_app
from fauxcall_reference_api.clock import ManualClock
from fauxcall_reference_api.settings import Settings

from .helpers import DEFAULT_INSTANT

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(DEFAULT_INSTANT)


@pytest.fixture
def settings() -> Settings:
    return Settings(latency=False)


@pytest.fixture
def app(settings: Settings, clock: ManualClock) -> FastAPI:
    return create_app(settings, clock=clock, sleep=clock.sleep)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
