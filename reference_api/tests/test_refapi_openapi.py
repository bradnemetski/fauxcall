from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from fauxcall_reference_api.app import create_app
from fauxcall_reference_api.openapi import refine_openapi
from fauxcall_reference_api.settings import Settings

ENDPOINTS = {
    "/healthz": {"get"},
    "/v1/exchanges": {"get"},
    "/v1/exchanges/{code}": {"get"},
    "/v1/tickers": {"get"},
    "/v1/tickers/{symbol}": {"get"},
    "/v1/tickers/{symbol}/quote": {"get"},
    "/v1/search": {"get"},
    "/v1/time": {"get"},
    "/v1/watchlists": {"get", "post"},
    "/v1/watchlists/{id}": {"get", "put", "patch", "delete"},
    "/v1/uploads": {"get", "post"},
    "/v1/uploads/{id}": {"get", "delete"},
    "/v1/uploads/{id}/content": {"get"},
    "/v1/stream/trades": {"get"},
}
ENVELOPE_REF = {"$ref": "#/components/schemas/ErrorEnvelope"}


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    """The served OpenAPI document (generated once for the whole module)."""
    response = TestClient(create_app(Settings(latency=False))).get("/openapi.json")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)
    return body


def operations(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
    ]


def test_info_and_paths(document: dict[str, Any]) -> None:
    assert document["info"]["title"] == "FauxCall Reference Market API"
    assert document["info"]["version"] == "1.0.0"
    assert "GROUND_TRUTH.md" in document["info"]["description"]
    assert {path: set(item) for path, item in document["paths"].items()} == ENDPOINTS


def test_security_scheme(document: dict[str, Any]) -> None:
    scheme = document["components"]["securitySchemes"]["ApiKeyAuth"]
    assert (scheme["type"], scheme["in"], scheme["name"]) == ("apiKey", "header", "X-API-Key")
    for path, _method, operation in operations(document):
        if path.startswith("/v1/"):
            assert operation["security"] == [{"ApiKeyAuth": []}]
        else:
            assert "security" not in operation


def test_errors_use_the_json_envelope_and_never_422(document: dict[str, Any]) -> None:
    schemas = document["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas
    detail = schemas["ErrorDetail"]
    assert set(detail["required"]) == {"code", "message", "request_id"}
    assert detail["properties"]["param"]["type"] == "string"
    for path, _method, operation in operations(document):
        responses = operation["responses"]
        assert "422" not in responses
        for status, response in responses.items():
            if status.startswith("4"):
                assert response["content"] == {"application/json": {"schema": ENVELOPE_REF}}
        if path.startswith("/v1/"):
            assert {"401", "403", "429"} <= set(responses)
    assert "400" in document["paths"]["/v1/tickers"]["get"]["responses"]
    assert "400" in document["paths"]["/v1/stream/trades"]["get"]["responses"]


def test_rate_limit_headers_are_documented(document: dict[str, Any]) -> None:
    assert set(document["components"]["headers"]) == {
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "Retry-After",
    }
    limit_ref = {"$ref": "#/components/headers/X-RateLimit-Limit"}
    for path, _method, operation in operations(document):
        for status, response in operation["responses"].items():
            headers = response.get("headers", {})
            if path.startswith("/v1/") and status != "401":
                assert headers["X-RateLimit-Limit"] == limit_ref
                assert ("Retry-After" in headers) == (status == "429")
            else:
                assert "X-RateLimit-Limit" not in headers


def test_contract_details(document: dict[str, Any]) -> None:
    paths = document["paths"]
    assert "Location" in paths["/v1/watchlists"]["post"]["responses"]["201"]["headers"]
    assert "application/json" in paths["/v1/uploads"]["post"]["responses"]["202"]["content"]
    sse = paths["/v1/stream/trades"]["get"]["responses"]["200"]["content"]
    assert "text/event-stream" in sse
    download = paths["/v1/uploads/{id}/content"]["get"]["responses"]["200"]["content"]
    assert "application/octet-stream" in download
    limit = next(p for p in paths["/v1/tickers"]["get"]["parameters"] if p["name"] == "limit")
    assert (limit["schema"]["minimum"], limit["schema"]["maximum"]) == (1, 100)
    assert limit["schema"]["default"] == 20
    assert "204" in paths["/v1/watchlists/{id}"]["delete"]["responses"]
    upload_schema = document["components"]["schemas"]["UploadOut"]
    assert "completed_at" not in upload_schema["required"]
    assert "label" in upload_schema["required"]


def test_docs_page() -> None:
    client = TestClient(create_app(Settings(latency=False)))
    assert "swagger-ui" in client.get("/docs").text
    assert client.get("/redoc").status_code == 404


def test_document_is_generated_once() -> None:
    app = create_app(Settings(latency=False))
    assert app.openapi() is app.openapi()


def test_refine_openapi_handles_minimal_documents() -> None:
    minimal: dict[str, Any] = {"paths": {"/x": {"get": {"responses": {"422": {}, "200": {}}}}}}
    refined = refine_openapi(minimal)
    assert refined["paths"]["/x"]["get"]["responses"] == {"200": {}}
    assert "X-RateLimit-Limit" in refined["components"]["headers"]
