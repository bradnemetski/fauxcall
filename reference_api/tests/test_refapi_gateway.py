from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from fauxcall_reference_api.app import create_app
from fauxcall_reference_api.gateway import is_api_path
from fauxcall_reference_api.settings import Settings
from fauxcall_reference_api.simulation import latency_ms

from .helpers import DEFAULT_INSTANT, DEMO, READONLY, REVOKED, SECOND, error_of

if TYPE_CHECKING:
    from fauxcall_reference_api.clock import ManualClock

RATE_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")


def test_is_api_path() -> None:
    assert is_api_path("/v1")
    assert is_api_path("/v1/tickers")
    assert not is_api_path("/v10/tickers")
    assert not is_api_path("/healthz")


@pytest.mark.parametrize(
    ("headers", "status", "code", "message"),
    [
        ({}, 401, "missing_api_key", "Missing API key. Send it in the X-API-Key header."),
        (
            {"X-API-Key": ""},
            401,
            "missing_api_key",
            "Missing API key. Send it in the X-API-Key header.",
        ),
        ({"X-API-Key": "nope"}, 401, "invalid_api_key", "Invalid API key."),
        ({"X-API-Key": "DEMO-KEY"}, 401, "invalid_api_key", "Invalid API key."),
        (REVOKED, 403, "api_key_revoked", "This API key has been revoked."),
    ],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/tickers"),
        ("POST", "/v1/watchlists"),
        ("DELETE", "/v1/watchlists/1001"),
        ("GET", "/v1/does-not-exist"),
        ("GET", "/v1"),
    ],
)
def test_rejected_credentials(
    client: TestClient,
    method: str,
    path: str,
    headers: dict[str, str],
    status: int,
    code: str,
    message: str,
) -> None:
    response = client.request(method, path, headers=headers)
    assert response.status_code == status
    assert error_of(response.json()) == {
        "code": code,
        "message": message,
        "request_id": response.json()["error"]["request_id"],
    }
    assert uuid.UUID(response.json()["error"]["request_id"]).version == 4
    assert not any(name in response.headers for name in RATE_HEADERS)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/watchlists"),
        ("PUT", "/v1/watchlists/1"),
        ("PATCH", "/v1/watchlists/1"),
        ("DELETE", "/v1/watchlists/1"),
        ("POST", "/v1/uploads"),
        ("DELETE", "/v1/uploads/x"),
        ("POST", "/v1/nope"),
    ],
)
def test_readonly_key_cannot_write(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, headers=READONLY, json={"name": "x"})
    assert response.status_code == 403
    error = error_of(response.json())
    assert error["code"] == "read_only_key"
    assert error["message"] == f"This API key is read-only; {method} requests are not allowed."
    assert response.headers["x-ratelimit-limit"] == "30"


def test_readonly_key_can_read(client: TestClient) -> None:
    for path in ("/v1/tickers", "/v1/watchlists", "/v1/uploads", "/v1/exchanges"):
        assert client.get(path, headers=READONLY).status_code == 200


@pytest.mark.parametrize("path", ["/healthz", "/openapi.json", "/docs"])
def test_public_paths_need_no_key_and_carry_no_rate_limit_headers(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert not any(name in response.headers for name in RATE_HEADERS)


def test_unknown_paths_outside_v1_are_404_without_auth(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert error_of(response.json())["code"] == "not_found"
    assert "x-ratelimit-limit" not in response.headers


def test_every_authenticated_response_carries_rate_limit_headers(client: TestClient) -> None:
    responses = [
        client.get("/v1/tickers", headers=DEMO),
        client.get("/v1/tickers/NOPE", headers=DEMO),  # 404
        client.get("/v1/tickers?limit=0", headers=DEMO),  # 400
        client.delete("/v1/tickers", headers=DEMO),  # 405
        client.get("/v1/nope", headers=DEMO),  # 404 route
        client.post("/v1/watchlists", headers=DEMO, json={"name": "x"}),  # 201
        client.delete("/v1/watchlists/1001", headers=DEMO),  # 204
        client.get("/v1/stream/trades?symbol=AAPL&count=1", headers=DEMO),  # SSE
    ]
    reset = str(-(-(DEFAULT_INSTANT + 10 * SECOND) // SECOND))
    for index, response in enumerate(responses, start=1):
        assert response.headers["x-ratelimit-limit"] == "30"
        assert response.headers["x-ratelimit-remaining"] == str(30 - index)
        assert response.headers["x-ratelimit-reset"] == reset
        assert "retry-after" not in response.headers


def test_rate_limit_window(client: TestClient, clock: ManualClock) -> None:
    for _ in range(30):
        assert client.get("/v1/time", headers=DEMO).status_code == 200
    clock.advance(4.5)
    limited = client.get("/v1/time", headers=DEMO)
    assert limited.status_code == 429
    error = error_of(limited.json())
    assert error["code"] == "rate_limited"
    assert error["message"] == "Rate limit exceeded: 30 requests per 10 seconds."
    assert limited.headers["retry-after"] == "6"
    assert limited.headers["x-ratelimit-remaining"] == "0"
    assert limited.headers["x-ratelimit-reset"] == str(DEFAULT_INSTANT // SECOND + 10)
    # Keys are limited independently; rejected requests do not extend the window.
    assert client.get("/v1/time", headers=READONLY).status_code == 200
    clock.advance(5.5)  # exactly 10 s after the window's first request
    renewed = client.get("/v1/time", headers=DEMO)
    assert renewed.status_code == 200
    assert renewed.headers["x-ratelimit-remaining"] == "29"
    assert renewed.headers["x-ratelimit-reset"] == str(DEFAULT_INSTANT // SECOND + 20)


def test_auth_comes_before_body_parsing_and_routing(client: TestClient) -> None:
    json_type = {"Content-Type": "application/json"}
    bad_body = client.post("/v1/watchlists", headers=json_type, content=b"{bad")
    assert error_of(bad_body.json())["code"] == "missing_api_key"
    unknown = client.delete("/v1/watchlists/999", headers=READONLY)
    assert error_of(unknown.json())["code"] == "read_only_key"


def test_rate_limit_counts_readonly_write_rejections(client: TestClient) -> None:
    client.post("/v1/watchlists", headers=READONLY, json={"name": "x"})
    response = client.get("/v1/tickers", headers=READONLY)
    assert response.headers["x-ratelimit-remaining"] == "28"


def test_custom_limits_and_keys(clock: ManualClock) -> None:
    settings = Settings(
        latency=False,
        api_keys={"k1": "full", "k2": "revoked"},
        rate_limit_requests=2,
        rate_limit_window_seconds=60,
    )
    with TestClient(create_app(settings, clock=clock, sleep=clock.sleep)) as client:
        assert client.get("/v1/time", headers={"X-API-Key": "k1"}).status_code == 200
        assert client.get("/v1/time", headers={"X-API-Key": "k1"}).status_code == 200
        limited = client.get("/v1/time", headers={"X-API-Key": "k1"})
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
        assert (
            error_of(limited.json())["message"] == "Rate limit exceeded: 2 requests per 60 seconds."
        )
        assert client.get("/v1/time", headers={"X-API-Key": "k2"}).status_code == 403
        assert client.get("/v1/time", headers=DEMO).status_code == 401


def test_latency_is_applied_per_method_and_path(clock: ManualClock) -> None:
    app = create_app(Settings(), clock=clock, sleep=clock.sleep)
    with TestClient(app) as client:
        client.get("/v1/tickers", headers=DEMO)
        client.get("/v1/tickers/AAPL", headers=DEMO)
        client.get("/v1/tickers/AAPL")  # 401s are delayed too
        client.delete("/v1/tickers/AAPL", headers=DEMO)
        client.get("/healthz")  # not delayed
        client.get("/openapi.json")  # not delayed
    assert clock.sleeps == [
        latency_ms("GET", "/v1/tickers") / 1000,
        latency_ms("GET", "/v1/tickers/AAPL") / 1000,
        latency_ms("GET", "/v1/tickers/AAPL") / 1000,
        latency_ms("DELETE", "/v1/tickers/AAPL") / 1000,
    ]
    assert all(0.015 <= seconds <= 0.060 for seconds in clock.sleeps)


def test_access_log(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="fauxcall_reference_api.access"):
        client.get("/v1/tickers/AAPL", headers=DEMO)
    (record,) = [r for r in caplog.records if r.name == "fauxcall_reference_api.access"]
    assert record.getMessage().startswith("GET /v1/tickers/AAPL -> 200 (")
    assert "request_id=" in record.getMessage()
