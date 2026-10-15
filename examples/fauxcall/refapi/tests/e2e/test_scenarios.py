"""End-to-end scenarios for this fake.

Each runs against the fake (default), the real API (``--target=real``) or both with a compliance
report (``--target=parity``). Write many: happy paths, parameter variations, errors, cross-endpoint
consistency (every reference resolves), time behavior, pagination walks, async jobs, auth failures.
Writes to the real API additionally need ``--allow-real-writes``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient


async def test_recorded_get_healthz(api: ApiClient) -> None:
    response = await api.get("/healthz")
    assert response.status_code == 200


async def test_recorded_get_v1_exchanges(api: ApiClient) -> None:
    response = await api.get("/v1/exchanges")
    assert response.status_code == 200


async def test_recorded_get_v1_exchanges_code(api: ApiClient) -> None:
    response = await api.get("/v1/exchanges/XNYS")
    assert response.status_code == 200


async def test_recorded_get_v1_tickers(api: ApiClient) -> None:
    response = await api.get("/v1/tickers")
    assert response.status_code == 200


async def test_recorded_get_v1_tickers_symbol(api: ApiClient) -> None:
    response = await api.get("/v1/tickers/AAPL")
    assert response.status_code == 200


async def test_recorded_get_v1_tickers_symbol_quote(api: ApiClient) -> None:
    response = await api.get("/v1/tickers/AAPL/quote")
    assert response.status_code == 200


async def test_recorded_get_v1_search(api: ApiClient) -> None:
    response = await api.get("/v1/search?q=apple")
    assert response.status_code == 200


async def test_recorded_get_v1_time(api: ApiClient) -> None:
    response = await api.get("/v1/time")
    assert response.status_code == 200


async def test_recorded_get_v1_watchlists(api: ApiClient) -> None:
    response = await api.get("/v1/watchlists")
    assert response.status_code == 200


async def test_recorded_get_v1_uploads(api: ApiClient) -> None:
    response = await api.get("/v1/uploads")
    assert response.status_code == 200


async def test_recorded_get_v1_stream_trades(api: ApiClient) -> None:
    response = await api.get("/v1/stream/trades?symbol=AAPL&count=3")
    assert response.status_code == 200
