from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from fauxcall_reference_api.errors import (
    ApiError,
    allowed_methods,
    describe_validation_error,
    envelope,
    handle_http_exception,
    handle_unexpected_error,
    handle_validation_error,
    request_id_of,
)

from .helpers import DEMO, error_of

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.testclient import TestClient


def make_request(app: FastAPI | None = None, method: str = "GET", path: str = "/x") -> Request:
    scope: dict[str, object] = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
        "state": {"request_id": "rid"},
        "app": app,
    }
    return Request(scope)


def test_envelope_shape() -> None:
    assert envelope("c", "m", "r") == {"error": {"code": "c", "message": "m", "request_id": "r"}}
    assert list(envelope("c", "m", "r", "p")["error"]) == ["code", "message", "request_id", "param"]


def test_request_id_falls_back_to_a_fresh_uuid() -> None:
    assert request_id_of({"state": {"request_id": "abc"}}) == "abc"
    assert len(request_id_of({})) == 36
    assert len(request_id_of({"state": {"request_id": 5}})) == 36


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            {"type": "json_invalid", "loc": ("body", 1)},
            ("invalid_json", "Request body is not valid JSON.", None),
        ),
        (
            {"type": "missing", "loc": ("body",)},
            ("invalid_body", "Request body must be a JSON object.", None),
        ),
        (
            {"type": "missing", "loc": ("query", "q")},
            ("missing_parameter", "Missing required parameter 'q'.", "q"),
        ),
        (
            {"type": "greater_than_equal", "loc": ("query", "limit")},
            (
                "invalid_parameter",
                "Invalid value for parameter 'limit': must be an integer between 1 and 100.",
                "limit",
            ),
        ),
        (
            {"type": "string_type", "loc": ("body", "symbols", 0)},
            (
                "invalid_parameter",
                "Invalid value for parameter 'symbols': must be an array of strings.",
                "symbols",
            ),
        ),
        (
            {"type": "int_parsing", "loc": ("query", "other")},
            ("invalid_parameter", "Invalid value for parameter 'other'.", "other"),
        ),
        (
            {"type": "value_error", "loc": ("weird",)},
            ("invalid_parameter", "Invalid value for parameter 'weird'.", "weird"),
        ),
        (
            {"type": "value_error", "loc": None},
            ("invalid_parameter", "Invalid value for parameter 'request'.", "request"),
        ),
    ],
)
def test_describe_validation_error(
    error: dict[str, object], expected: tuple[str, str, str | None]
) -> None:
    assert describe_validation_error(error) == expected


def test_validation_handler_without_errors_still_answers_400() -> None:
    response = asyncio.run(handle_validation_error(make_request(), RequestValidationError([])))
    assert response.status_code == 400


def test_allowed_methods_ignores_non_api_routes() -> None:
    assert allowed_methods([], "/x") == []


def test_other_http_exceptions_use_their_detail(app: FastAPI) -> None:
    request = make_request(app)
    response = asyncio.run(
        handle_http_exception(request, StarletteHTTPException(400, "Bad multipart"))
    )
    assert response.status_code == 400
    assert response.body == (
        b'{"error":{"code":"bad_request","message":"Bad multipart","request_id":"rid"}}'
    )
    teapot = asyncio.run(handle_http_exception(request, StarletteHTTPException(418)))
    assert b'"code":"http_error"' in teapot.body


def test_unexpected_errors_are_500(caplog: pytest.LogCaptureFixture) -> None:
    response = asyncio.run(handle_unexpected_error(make_request(), RuntimeError("boom")))
    assert response.status_code == 500
    assert response.body == (
        b'{"error":{"code":"internal_error","message":"An unexpected error occurred.",'
        b'"request_id":"rid"}}'
    )
    assert "unhandled error on GET /x" in caplog.text


def test_api_error_keeps_headers() -> None:
    error = ApiError(409, "c", "m", headers={"X-A": "1"})
    assert (error.status, error.code, error.message, error.param, error.headers) == (
        409,
        "c",
        "m",
        None,
        {"X-A": "1"},
    )


def test_unknown_route(client: TestClient) -> None:
    response = client.get("/v1/tickers/AAPL/nope", headers=DEMO)
    assert response.status_code == 404
    assert error_of(response.json())["message"] == "No route matches GET /v1/tickers/AAPL/nope."


def test_trailing_slashes_are_not_redirected(client: TestClient) -> None:
    response = client.get("/v1/tickers/", headers=DEMO, follow_redirects=False)
    assert response.status_code == 404
    assert error_of(response.json())["code"] == "not_found"


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("DELETE", "/v1/tickers", "GET"),
        ("HEAD", "/v1/tickers", "GET"),
        ("PUT", "/v1/watchlists", "GET, POST"),
        ("POST", "/v1/watchlists/1001", "GET, PUT, PATCH, DELETE"),
        ("POST", "/v1/uploads/abc", "GET, DELETE"),
        ("POST", "/healthz", "GET"),
    ],
)
def test_method_not_allowed(client: TestClient, method: str, path: str, allow: str) -> None:
    response = client.request(method, path, headers=DEMO)
    assert response.status_code == 405
    assert response.headers["allow"] == allow
    if method != "HEAD":
        error = error_of(response.json())
        assert error["code"] == "method_not_allowed"
        assert error["message"] == f"Method {method} is not allowed for {path}."


def test_validation_errors_are_400_not_422(client: TestClient) -> None:
    response = client.get("/v1/search?q=a", headers=DEMO)
    assert response.status_code == 400
    error = error_of(response.json())
    assert error["code"] == "invalid_parameter"
    assert error["param"] == "q"
    assert (
        error["message"]
        == "Invalid value for parameter 'q': must be a string of 2 to 64 characters."
    )


def test_body_errors(client: TestClient) -> None:
    json_type = {**DEMO, "Content-Type": "application/json"}
    bad_json = client.post("/v1/watchlists", headers=json_type, content=b"{bad")
    assert error_of(bad_json.json())["code"] == "invalid_json"
    assert "param" not in bad_json.json()["error"]
    for content, headers in [(b"[1]", json_type), (b'{"name":"x"}', DEMO), (b"", DEMO)]:
        response = client.post("/v1/watchlists", headers=headers, content=content)
        assert response.status_code == 400
        assert error_of(response.json()) == {
            "code": "invalid_body",
            "message": "Request body must be a JSON object.",
            "request_id": response.json()["error"]["request_id"],
        }
