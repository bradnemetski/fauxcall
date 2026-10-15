"""Watchlists: full write cycles and every refusal (writes: run with --allow-real-writes).

Watchlists are shared by all keys on the real API, so every scenario deletes what it created.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx2
    from fauxcall.testing import ApiClient

import contextlib

import pytest

NAME = "fauxcall-e2e"


def _error(response: httpx2.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body


@contextlib.asynccontextmanager
async def _watchlist(api: ApiClient, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """A created watchlist, deleted afterwards whatever happens."""
    created = await api.post("/v1/watchlists", json=body)
    assert created.status_code == 201, created.text
    record: dict[str, Any] = created.json()
    try:
        yield record
    finally:
        await api.delete(f"/v1/watchlists/{record['id']}")


async def test_watchlist_full_cycle(api: ApiClient) -> None:
    body = {"name": f"{NAME}-cycle", "symbols": ["nvda", "AMD", "nvda", "7267.t"], "note": "x"}
    async with _watchlist(api, body) as created:
        assert created["symbols"] == ["NVDA", "AMD", "7267.T"]
        assert created["created_at"] == created["updated_at"]
        path = f"/v1/watchlists/{created['id']}"
        assert (await api.get(path)).json() == created
        listed = (await api.get("/v1/watchlists")).json()
        assert created in listed
        replaced = (await api.put(path, json={"name": f"{NAME}-put", "symbols": []})).json()
        assert (replaced["name"], replaced["symbols"]) == (f"{NAME}-put", [])
        assert replaced["created_at"] == created["created_at"]
        renamed = (await api.patch(path, json={"name": f"{NAME}-patch"})).json()
        assert renamed["symbols"] == []
        refilled = (await api.patch(path, json={"symbols": ["shel.l", "BP.L"]})).json()
        assert (refilled["name"], refilled["symbols"]) == (f"{NAME}-patch", ["SHEL.L", "BP.L"])
        touched = (await api.patch(path, json={})).json()
        assert touched["updated_at"] >= refilled["updated_at"]
        assert (await api.get(path)).json() == touched
    assert (await api.get(path)).status_code == 404
    again = await api.delete(path)
    assert (again.status_code, _error(again)["code"]) == (404, "watchlist_not_found")


async def test_watchlist_symbols_resolve(api: ApiClient) -> None:
    async with _watchlist(api, {"name": f"{NAME}-refs", "symbols": ["6758.t", "8035.T"]}) as wl:
        for symbol in wl["symbols"]:
            assert (await api.get(f"/v1/tickers/{symbol}")).json()["symbol"] == symbol
            quote = (await api.get(f"/v1/tickers/{symbol}/quote")).json()
            assert quote["currency"] == "JPY"


async def test_ids_count_up_and_lists_are_sorted(api: ApiClient) -> None:
    async with (
        _watchlist(api, {"name": f"{NAME}-a"}) as first,
        _watchlist(api, {"name": f"{NAME}-b", "symbols": ["v"]}) as second,
    ):
        assert second["id"] == first["id"] + 1
        listed = (await api.get("/v1/watchlists")).json()
        assert [w["id"] for w in listed] == sorted(w["id"] for w in listed)
        assert (await api.get(f"/v1/watchlists/0{first['id']}")).json() == first


@pytest.mark.parametrize(
    ("body", "code", "param"),
    [
        ({}, "missing_parameter", "name"),
        ({"name": "", "symbols": "x"}, "invalid_parameter", "name"),
        ({"name": "n" * 51}, "invalid_parameter", "name"),
        ({"name": None}, "invalid_parameter", "name"),
        ({"name": f"{NAME}-x", "symbols": "AAPL"}, "invalid_parameter", "symbols"),
        ({"name": f"{NAME}-x", "symbols": None}, "invalid_parameter", "symbols"),
        ({"name": f"{NAME}-x", "symbols": ["AAPL", "brk.b"]}, "unknown_symbol", "symbols"),
    ],
)
async def test_create_refusals(api: ApiClient, body: dict[str, Any], code: str, param: str) -> None:
    response = await api.post("/v1/watchlists", json=body)
    assert response.status_code == 400
    assert (_error(response)["code"], _error(response)["param"]) == (code, param)


async def test_create_body_refusals(api: ApiClient) -> None:
    for content, content_type, code in [
        (b"{nope", "application/json", "invalid_json"),
        (b'["fauxcall-e2e"]', "application/json", "invalid_body"),
        (b'{"name": "fauxcall-e2e-text"}', "text/plain", "invalid_body"),
    ]:
        response = await api.post(
            "/v1/watchlists", content=content, headers={"content-type": content_type}
        )
        assert (response.status_code, _error(response)["code"]) == (400, code)


async def test_update_refusals(api: ApiClient) -> None:
    async with _watchlist(api, {"name": f"{NAME}-upd", "symbols": ["KO"]}) as wl:
        path = f"/v1/watchlists/{wl['id']}"
        no_symbols = await api.put(path, json={"name": f"{NAME}-upd"})
        assert (_error(no_symbols)["code"], _error(no_symbols)["param"]) == (
            "missing_parameter",
            "symbols",
        )
        null_name = await api.patch(path, json={"name": None})
        assert _error(null_name)["param"] == "name"
        unknown = await api.patch(path, json={"symbols": ["PEP", "COKE"]})
        assert _error(unknown)["message"] == "Unknown symbol 'COKE'."
        assert (await api.get(path)).json()["symbols"] == ["KO"]  # nothing changed
    gone = await api.patch("/v1/watchlists/987654", json={"symbols": ["COKE"]})
    assert _error(gone)["code"] == "watchlist_not_found"
    bad_id = await api.put("/v1/watchlists/x1", json={})
    assert _error(bad_id)["param"] == "id"
