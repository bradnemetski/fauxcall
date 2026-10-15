"""Every error the real API produces: parameters, ordering of checks, unknown ids, auth, routing.

Read-only (GET) except where noted; writes that are refused still need ``--allow-real-writes``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fauxcall.model.fields import RandomPolicy
from fauxcall.testing import CompareOptions

if TYPE_CHECKING:
    import httpx2
    from fauxcall.testing import ApiClient

UNKNOWN_UPLOAD = "8e1f0a52-6c3d-4b7e-9f21-0a1b2c3d4e5f"
UNROUTED = CompareOptions(
    policies=[RandomPolicy(path="error.request_id", scope="body", generator="uuid4")]
)
"""Unknown routes belong to no endpoint, so the model's request_id policy is given per call."""


def _error(response: httpx2.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    assert len(body["request_id"]) == 36
    return body


async def test_ticker_listing_parameters(api: ApiClient) -> None:
    for params, code, param in [
        ({"limit": "0"}, "invalid_parameter", "limit"),
        ({"limit": "101"}, "invalid_parameter", "limit"),
        ({"limit": "ten"}, "invalid_parameter", "limit"),
        ({"limit": "2.5"}, "invalid_parameter", "limit"),
        ({"limit": ""}, "invalid_parameter", "limit"),
        ({"exchange": "XASX"}, "invalid_parameter", "exchange"),
        ({"exchange": "xetr"}, "invalid_parameter", "exchange"),
        ({"sector": "Space"}, "invalid_parameter", "sector"),
        ({"cursor": "%%%"}, "invalid_cursor", "cursor"),
        ({"cursor": "eyJhZnRlciI6IkJSSy5CIn0"}, "invalid_cursor", "cursor"),
        ({"cursor": "e30"}, "invalid_cursor", "cursor"),
    ]:
        response = await api.get("/v1/tickers", params=params)
        assert response.status_code == 400
        error = _error(response)
        assert (error["code"], error["param"]) == (code, param)


async def test_ticker_listing_check_order(api: ApiClient) -> None:
    """limit, then exchange, then sector, then cursor."""
    for params, param in [
        ({"limit": "-5", "exchange": "NOPE", "sector": "Space", "cursor": "x"}, "limit"),
        ({"exchange": "NOPE", "sector": "Space", "cursor": "x"}, "exchange"),
        ({"sector": "Space", "cursor": "x"}, "sector"),
        ({"cursor": "x", "limit": "3"}, "cursor"),
    ]:
        assert _error(await api.get("/v1/tickers", params=params))["param"] == param


async def test_search_parameters(api: ApiClient) -> None:
    missing = await api.get("/v1/search")
    assert (_error(missing)["code"], _error(missing)["param"]) == ("missing_parameter", "q")
    for query in ["", "x", "y" * 65]:
        response = await api.get("/v1/search", params={"q": query})
        assert _error(response)["code"] == "invalid_parameter"


async def test_unknown_exchanges_and_tickers(api: ApiClient) -> None:
    for path, code in [
        ("/v1/exchanges/XASX", "exchange_not_found"),
        ("/v1/exchanges/xetr", "exchange_not_found"),
        ("/v1/tickers/BRK.B", "ticker_not_found"),
        ("/v1/tickers/brk.b", "ticker_not_found"),
        ("/v1/tickers/sap/quote", "ticker_not_found"),
        ("/v1/tickers/AAPL.US/quote", "ticker_not_found"),
    ]:
        response = await api.get(path)
        assert response.status_code == 404
        assert _error(response)["code"] == code


async def test_trade_stream_parameters(api: ApiClient) -> None:
    for params, code, param in [
        ({"symbol": "MSFT", "count": "0"}, "invalid_parameter", "count"),
        ({"symbol": "MSFT", "count": "51"}, "invalid_parameter", "count"),
        ({"symbol": "MSFT", "count": "five"}, "invalid_parameter", "count"),
        ({"count": "3"}, "missing_parameter", "symbol"),
        ({"symbol": "brk.b"}, "unknown_symbol", "symbol"),
        ({"symbol": "BRK.B", "count": "-1"}, "invalid_parameter", "count"),
    ]:
        response = await api.get("/v1/stream/trades", params=params)
        assert response.status_code == 400
        error = _error(response)
        assert (error["code"], error["param"]) == (code, param)


async def test_watchlist_and_upload_ids(api: ApiClient) -> None:
    for path, status, code in [
        ("/v1/watchlists/abc", 400, "invalid_parameter"),
        ("/v1/watchlists/7.0", 404, "watchlist_not_found"),  # Pydantic lax ints: '7.0' is 7
        ("/v1/watchlists/0", 404, "watchlist_not_found"),
        ("/v1/watchlists/-3", 404, "watchlist_not_found"),
        ("/v1/watchlists/00042", 404, "watchlist_not_found"),
        (f"/v1/uploads/{UNKNOWN_UPLOAD}", 404, "upload_not_found"),
        (f"/v1/uploads/{UNKNOWN_UPLOAD.upper()}", 404, "upload_not_found"),
        ("/v1/uploads/not-an-id", 404, "upload_not_found"),
        (f"/v1/uploads/{UNKNOWN_UPLOAD}/content", 404, "upload_not_found"),
    ]:
        response = await api.get(path)
        assert response.status_code == status
        assert _error(response)["code"] == code


async def test_missing_invalid_and_revoked_keys(api: ApiClient) -> None:
    for key, status, code in [
        ("", 401, "missing_api_key"),
        ("definitely-not-a-key", 401, "invalid_api_key"),
        ("revoked-key", 403, "api_key_revoked"),
    ]:
        for path in ["/v1/exchanges", "/v1/tickers?limit=zero", "/v1/watchlists/abc"]:
            response = await api.get(path, headers={"X-API-Key": key})
            assert response.status_code == status
            assert _error(response)["code"] == code


async def test_read_only_key_reads(api: ApiClient) -> None:
    readonly = {"X-API-Key": "readonly-key"}
    assert (await api.get("/v1/watchlists", headers=readonly)).status_code == 200
    assert (await api.get("/v1/time", headers=readonly)).json()["zone"] == "UTC"
    missing = await api.get("/v1/exchanges/XXX", headers=readonly)
    assert _error(missing)["code"] == "exchange_not_found"


async def test_read_only_key_cannot_write(api: ApiClient) -> None:
    """Refused before anything is checked: nothing is created (writes: needs approval)."""
    readonly = {"X-API-Key": "readonly-key"}
    created = await api.post("/v1/watchlists", json={"name": "fauxcall-e2e-ro"}, headers=readonly)
    assert (created.status_code, _error(created)["code"]) == (403, "read_only_key")
    removed = await api.delete("/v1/watchlists/abc", headers=readonly)
    assert (
        _error(removed)["message"] == "This API key is read-only; DELETE requests are not allowed."
    )
    revoked = await api.post("/v1/uploads", json={}, headers={"X-API-Key": "revoked-key"})
    assert _error(revoked)["code"] == "api_key_revoked"


async def test_routing(api: ApiClient) -> None:
    for path in ["/v1/tickers/", "/V1/tickers", "/v1/Tickers/AAPL", "/v1/nothing-here", "/nope"]:
        response = await api.get(path, compare=UNROUTED)
        assert response.status_code == 404
        assert _error(response)["code"] == "not_found"
        assert _error(response)["message"] == f"No route matches GET {path}."
    for path, allow in [
        ("/v1/tickers", "GET"),
        ("/v1/watchlists", "GET, POST"),
        ("/v1/watchlists/5", "GET, PUT, PATCH, DELETE"),
        ("/v1/uploads/x", "GET, DELETE"),
    ]:
        refused = await api.request("OPTIONS", path, compare=UNROUTED)
        assert (refused.status_code, _error(refused)["code"]) == (405, "method_not_allowed")
        assert refused.headers["allow"] == allow
    head = await api.request("HEAD", "/v1/time", compare=UNROUTED)
    assert (head.status_code, head.content) == (405, b"")


async def test_wrong_methods_outside_v1(api: ApiClient) -> None:
    """Outside /v1 a 405 lists Allow too, with no key check or rate limit (000854-000857).

    Allow names the API's endpoints only, so /openapi.json's is empty.
    """
    for path, allow in [("/healthz", "GET"), ("/openapi.json", "")]:
        refused = await api.request("OPTIONS", path, compare=UNROUTED)
        assert (refused.status_code, _error(refused)["code"]) == (405, "method_not_allowed")
        assert refused.headers["allow"] == allow
        assert "x-ratelimit-limit" not in refused.headers
    keyless = await api.request("OPTIONS", "/healthz", headers={"X-API-Key": ""}, compare=UNROUTED)
    assert (keyless.status_code, keyless.headers["allow"]) == (405, "GET")
    head = await api.request("HEAD", "/healthz", compare=UNROUTED)
    assert (head.status_code, head.content, head.headers["allow"]) == (405, b"", "GET")


async def test_unknown_routes_need_a_key_first(api: ApiClient) -> None:
    """Under /v1 the key is checked before the route (000025, 000026)."""
    keyless = await api.get("/v1/nothing-here", headers={"X-API-Key": ""}, compare=UNROUTED)
    assert (keyless.status_code, _error(keyless)["code"]) == (401, "missing_api_key")
    revoked = await api.get(
        "/v1/nothing-here", headers={"X-API-Key": "revoked-key"}, compare=UNROUTED
    )
    root = await api.get("/v1", headers={"X-API-Key": ""}, compare=UNROUTED)
    assert (root.status_code, _error(root)["code"]) == (401, "missing_api_key")
    beside = await api.get("/v1x", headers={"X-API-Key": ""}, compare=UNROUTED)
    assert (beside.status_code, _error(beside)["code"]) == (404, "not_found")
    head = await api.request(
        "HEAD", "/v1/exchanges", headers={"X-API-Key": "revoked-key"}, compare=UNROUTED
    )
    assert (head.status_code, head.content) == (403, b"")
    assert (revoked.status_code, _error(revoked)["code"]) == (403, "api_key_revoked")
