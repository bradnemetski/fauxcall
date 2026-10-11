from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from fauxcall.errors import SpecError
from fauxcall.jsonfmt import JsonFloat
from fauxcall.model.api import RealTarget
from fauxcall.model.endpoints import EndpointModel, ParamModel, RequestBodyModel, ResponseVariant
from fauxcall.spec import openapi
from fauxcall.spec.loader import parse_spec_text
from fauxcall.spec.openapi import (
    OpenApiDocument,
    auth_from_spec,
    endpoints_from_spec,
    suggest_role,
    summarize,
)
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from fauxcall.model.endpoints import EndpointRole
    from fauxcall.types import JsonValue

FIXTURES = Path(__file__).parent / "fixtures" / "specs"
SPEC_URLS = {
    "petstore3-openapi.json": "https://petstore3.swagger.io/api/v3/openapi.json",
    "timeapi-swagger.json": "https://timeapi.io/swagger/v1/swagger.json",
    "frankfurter-openapi.json": "https://api.frankfurter.dev/v1/openapi.json",
    "fakerestapi-swagger.json": "https://fakerestapi.azurewebsites.net/swagger/v1/swagger.json",
    "httpbin-spec.json": "https://httpbin.org/spec.json",
}


def load(name: str, *, fetched: bool = True) -> OpenApiDocument:
    spec = parse_spec_text((FIXTURES / name).read_text(encoding="utf-8"), source=name)
    return OpenApiDocument(spec, spec_url=SPEC_URLS.get(name) if fetched else None)


def inline(spec: dict[str, JsonValue], spec_url: str | None = None) -> OpenApiDocument:
    return OpenApiDocument(
        {"openapi": "3.1.0", "info": {"title": "Inline", "version": "1"}, **spec}, spec_url=spec_url
    )


def by_id(endpoints: list[EndpointModel], endpoint_id: str) -> EndpointModel:
    return next(endpoint for endpoint in endpoints if endpoint.id == endpoint_id)


def dig(value: Any, *keys: str | int) -> Any:
    for key in keys:
        value = value[key]
    return value


def endpoint(
    method: str,
    path: str,
    *,
    responses: list[tuple[int, str | None, JsonValue]] | None = None,
    request_types: list[str] | None = None,
    params: list[tuple[str, str]] | None = None,
    spec_path: str | None = None,
    success_status: int = 200,
) -> EndpointModel:
    return EndpointModel(
        id=f"{method} {path}",
        method=HttpMethod(method),
        path=path,
        spec_path=spec_path,
        responses=[
            ResponseVariant(status=status, content_type=content_type, documented_schema=schema)
            for status, content_type, schema in responses or []
        ],
        request_body=RequestBodyModel(content_types=request_types) if request_types else None,
        params=[
            ParamModel(name=name, location="query" if location == "query" else "header")
            for name, location in params or []
        ],
        success_status=success_status,
    )


# ---------------------------------------------------------------------------
# Real specs
# ---------------------------------------------------------------------------


def test_petstore3_document() -> None:
    doc = load("petstore3-openapi.json")
    assert doc.version == "3.0.4"
    assert not doc.is_swagger2
    assert doc.title == "Swagger Petstore - OpenAPI 3.0"
    assert doc.api_version == "1.0.27"
    assert doc.real_target() == RealTarget(
        base_url="https://petstore3.swagger.io", base_path="/api/v3"
    )
    operations = list(doc.operations())
    assert len(operations) == 19
    assert (operations[0].method, operations[0].path) == (HttpMethod.PUT, "/pet")
    assert doc.notes == []


def test_petstore3_endpoints() -> None:
    endpoints = endpoints_from_spec(load("petstore3-openapi.json"))
    assert len(endpoints) == 19
    ids = [e.id for e in endpoints]
    assert ids[:3] == ["PUT /api/v3/pet", "POST /api/v3/pet", "GET /api/v3/pet/findByStatus"]
    assert "GET /api/v3/pet/{petId}" in ids
    assert "DELETE /api/v3/user/{username}" in ids

    get_pet = by_id(endpoints, "GET /api/v3/pet/{petId}")
    assert (get_pet.path, get_pet.spec_path) == ("/api/v3/pet/{petId}", "/pet/{petId}")
    assert (get_pet.operation_id, get_pet.summary) == ("getPetById", "Find pet by ID.")
    assert get_pet.tags == ["pet"]
    assert get_pet.role == "replay"
    assert get_pet.success_status == 200
    (pet_id,) = get_pet.params
    assert (pet_id.name, pet_id.location, pet_id.required) == ("petId", "path", True)
    assert pet_id.param_schema == {"type": "integer", "format": "int64"}
    assert [r.status for r in get_pet.responses] == [200, 400, 404]
    ok = get_pet.responses[0]
    assert (ok.description, ok.content_type) == ("successful operation", "application/json")
    assert dig(ok.documented_schema, "required") == ["name", "photoUrls"]
    assert dig(ok.documented_schema, "properties", "category", "properties", "name", "type") == (
        "string"
    )
    assert "$ref" not in str(ok.documented_schema)
    assert ok.headers == {}
    assert "documented 'default' response: Unexpected error (not a single status)" in (
        get_pet.notes
    )

    update = by_id(endpoints, "PUT /api/v3/pet")
    assert update.request_body is not None
    assert update.request_body.content_types == [
        "application/json",
        "application/xml",
        "application/x-www-form-urlencoded",
    ]
    assert update.request_body.required
    assert dig(update.request_body.body_schema, "properties", "tags", "items", "type") == "object"

    by_status = by_id(endpoints, "GET /api/v3/pet/findByStatus")
    (status,) = by_status.params
    assert (status.required, status.explode, status.style, status.default) == (
        True,
        True,
        None,
        "available",
    )

    upload = by_id(endpoints, "POST /api/v3/pet/{petId}/uploadImage")
    assert upload.request_body == RequestBodyModel(
        content_types=["application/octet-stream"],
        body_schema={"type": "string", "format": "binary"},
        required=False,
    )
    login = by_id(endpoints, "GET /api/v3/user/login")
    assert "200 response documents headers: X-Rate-Limit, X-Expires-After" in login.notes
    delete = by_id(endpoints, "DELETE /api/v3/pet/{petId}")
    assert [(p.name, p.location) for p in delete.params] == [
        ("api_key", "header"),
        ("petId", "path"),
    ]


def test_petstore3_auth() -> None:
    auth = auth_from_spec(load("petstore3-openapi.json"))
    assert auth is not None
    oauth, api_key = auth.schemes
    assert (oauth.kind, oauth.prefix) == ("oauth2", "Bearer ")
    assert oauth.oauth is not None
    assert oauth.oauth.scopes == ["write:pets", "read:pets"]
    assert (api_key.kind, api_key.location, api_key.name) == ("api_key", "header", "api_key")
    assert auth.required_for == [
        "PUT /api/v3/pet",
        "POST /api/v3/pet",
        "GET /api/v3/pet/findByStatus",
        "GET /api/v3/pet/findByTags",
        "GET /api/v3/pet/{petId}",
        "POST /api/v3/pet/{petId}",
        "DELETE /api/v3/pet/{petId}",
        "POST /api/v3/pet/{petId}/uploadImage",
        "GET /api/v3/store/inventory",
    ]
    assert auth.exempt == []


def test_timeapi_without_servers_uses_the_spec_origin() -> None:
    doc = load("timeapi-swagger.json")
    assert doc.version == "3.0.1"
    assert doc.real_target() == RealTarget(base_url="https://timeapi.io", base_path="")
    endpoints = endpoints_from_spec(doc)
    assert len(endpoints) == 12
    zone = by_id(endpoints, "GET /api/v1/time/current/zone")
    ok, invalid = zone.responses
    assert (ok.status, ok.content_type, ok.documented_schema) == (200, None, None)
    assert (invalid.status, invalid.content_type) == (400, "application/json")
    assert dig(invalid.documented_schema, "properties", "status", "type") == "integer"
    assert auth_from_spec(doc) is None


def test_frankfurter_openapi_31() -> None:
    doc = load("frankfurter-openapi.json")
    assert doc.version == "3.1.2"
    assert doc.real_target() == RealTarget(base_url="https://api.frankfurter.dev", base_path="/v1")
    endpoints = endpoints_from_spec(doc)
    assert [e.id for e in endpoints] == [
        "GET /v1/latest",
        "GET /v1/{date}",
        "GET /v1/{start_date}..",
        "GET /v1/{start_date}..{end_date}",
        "GET /v1/currencies",
    ]
    latest = endpoints[0]
    base, symbols = latest.params
    assert (base.name, base.default) == ("base", "EUR")  # the default sits next to an allOf
    assert dig(base.param_schema, "allOf", 0, "type") == "string"
    assert (symbols.explode, dig(symbols.param_schema, "items", "enum", 0)) == (False, "AUD")
    multiple_of = dig(
        latest.responses[0].documented_schema,
        "properties",
        "rates",
        "additionalProperties",
        "multipleOf",
    )
    assert isinstance(multiple_of, JsonFloat)
    assert multiple_of.lexeme == "0.00001"
    period = by_id(endpoints, "GET /v1/{start_date}..{end_date}")
    assert [(p.name, p.required) for p in period.params][:2] == [
        ("start_date", True),
        ("end_date", True),
    ]
    assert period.responses[1].description == "Resource not found"


def test_fakerestapi_prefers_json_content_types() -> None:
    endpoints = endpoints_from_spec(load("fakerestapi-swagger.json"))
    assert len(endpoints) == 27
    listing = by_id(endpoints, "GET /api/v1/Activities")
    assert listing.responses[0].content_type == "application/json; v=1.0"
    assert dig(listing.responses[0].documented_schema, "items", "properties", "id", "type") == (
        "integer"
    )
    create = by_id(endpoints, "POST /api/v1/Activities")
    assert create.request_body is not None
    assert create.request_body.content_types == [
        "application/json; v=1.0",
        "text/json; v=1.0",
        "application/*+json; v=1.0",
    ]
    assert not create.request_body.required


def test_httpbin_swagger2() -> None:
    doc = load("httpbin-spec.json")
    assert (doc.version, doc.is_swagger2, doc.title) == ("2.0", True, "httpbin.org")
    assert doc.real_target() == RealTarget(base_url="https://httpbin.org", base_path="")
    endpoints = endpoints_from_spec(doc)
    assert len(endpoints) == 78
    assert {e.method for e in endpoints} >= {HttpMethod.TRACE, HttpMethod.PATCH}

    redirect = by_id(endpoints, "GET /absolute-redirect/{n}")
    (n,) = redirect.params
    assert (n.param_schema, n.required) == ({"type": "integer"}, True)
    assert "parameter 'n' declares non-standard type 'int' (read as 'integer')" in redirect.notes
    assert (redirect.responses[0].status, redirect.responses[0].content_type) == (302, None)

    form = by_id(endpoints, "POST /redirect-to")
    assert form.params == []
    assert form.request_body == RequestBodyModel(
        content_types=["application/x-www-form-urlencoded"],
        body_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}, "status_code": {"type": "integer"}},
            "required": ["url"],
        },
        required=True,
    )
    assert by_id(endpoints, "GET /image/png").responses[0].content_type == "image/png"
    assert by_id(endpoints, "GET /image").responses[0].content_type == "image/webp"
    get = by_id(endpoints, "GET /get").responses[0]
    assert (get.content_type, get.documented_schema) == ("application/json", None)
    # Swagger 2.0 has no rule against an Authorization header parameter: it is kept.
    assert [p.name for p in by_id(endpoints, "GET /bearer").params] == ["Authorization"]
    drip = by_id(endpoints, "GET /drip")
    assert {p.name: p.default for p in drip.params} == {
        "duration": 2,
        "numbytes": 10,
        "code": 200,
        "delay": 2,
    }
    assert drip.params[0].param_schema == {"type": "number", "default": 2}
    (freeform,) = by_id(endpoints, "GET /response-headers").params
    assert (freeform.style, freeform.explode) == ("form", True)
    assert freeform.param_schema == {"additionalProperties": {"type": "string"}, "type": "object"}
    codes = by_id(endpoints, "GET /status/{codes}").params[0]
    assert (codes.param_schema, codes.required) == (None, True)
    assert auth_from_spec(doc) is None


def test_yaml_spec_with_refs_cycles_and_path_level_parameters() -> None:
    doc = load("refs-openapi.yaml", fetched=False)
    assert doc.real_target() == RealTarget(base_url="https://eu.api.example.com", base_path="/v2")
    assert doc.notes == ["using the first of 2 servers; others: https://sandbox.example.com/v2"]
    endpoints = endpoints_from_spec(doc)
    assert [e.id for e in endpoints] == [
        "GET /v2/things",
        "POST /v2/things",
        "GET /v2/things/{thingId}",
        "DELETE /v2/things/{thingId}",
        "POST /v2/things/{thingId}/attachments",
        "GET /v2/things/{thingId}/events",
        "GET /v2/health",
    ]
    listing, create, get, delete, *_ = endpoints

    limit, tag = listing.params
    assert (limit.default, dig(limit.param_schema, "maximum")) == (20, 100)
    assert (tag.style, tag.explode) == ("form", False)
    assert listing.notes == [
        "external $ref 'common.yaml#/components/schemas/Metadata' is not resolved",
        "200 response documents headers: X-Total-Count, Link",
        "documented 'default' response: Something went wrong (not a single status)",
    ]

    assert create.summary == "Create a thing."
    assert create.request_body is not None
    assert create.request_body.required
    new_thing = create.request_body.body_schema
    assert list(dig(new_thing, "properties")) == ["name", "on", "created", "nickname", "metadata"]
    assert dig(new_thing, "properties", "nickname") == {"type": "string", "maxLength": 40}
    assert dig(new_thing, "properties", "metadata") == {
        "$ref": "common.yaml#/components/schemas/Metadata"
    }
    assert "external $ref 'common.yaml#/components/schemas/Metadata' is not resolved" in (
        create.notes
    )
    assert "documented '4XX' response: Client error (not a single status)" in create.notes
    assert create.success_status == 201

    assert [(p.name, p.location, p.required) for p in get.params] == [
        ("thingId", "path", True),
        ("X-Request-Id", "header", True),
    ]
    assert get.params[1].param_schema == {"type": "string", "format": "uuid"}
    assert (
        "header parameter 'Accept' ignored (OpenAPI leaves it to content negotiation and auth)"
        in get.notes
    )
    thing = get.responses[0].documented_schema
    assert dig(thing, "allOf", 0, "required") == ["name"]  # NewThing, expanded
    cycle = {"$ref": "#/components/schemas/Thing"}
    assert dig(thing, "allOf", 1, "properties", "parent") == cycle
    assert dig(thing, "allOf", 1, "properties", "children", "items") == cycle
    not_found = get.responses[1]
    assert (not_found.content_type, not_found.description) == (
        "application/problem+json",
        "Something went wrong",
    )

    assert delete.deprecated
    assert [(p.name, p.required) for p in delete.params] == [
        ("thingId", True),
        ("X-Request-Id", False),
    ]
    assert (delete.success_status, delete.responses[0].content_type) == (204, None)

    auth = auth_from_spec(doc)
    assert auth is not None
    assert [(s.kind, s.location, s.name, s.prefix) for s in auth.schemes] == [
        ("api_key", "header", "X-API-Key", None),
        ("bearer", None, None, "Bearer "),
        ("oauth2", None, None, "Bearer "),
    ]
    assert auth.schemes[2].oauth is not None
    assert auth.schemes[2].oauth.scopes == ["things:read", "things:write", "admin"]
    assert (auth.required_for, auth.exempt) == (["*"], ["GET /v2/health"])


# ---------------------------------------------------------------------------
# Versions and locations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"openapi": "4.0.0"}, "unsupported OpenAPI version '4.0.0'"),
        ({"swagger": "1.2"}, "unsupported Swagger version '1.2'"),
        ({"info": {"title": "x"}}, "no 'openapi' or 'swagger' field"),
    ],
)
def test_unsupported_documents(spec: dict[str, JsonValue], message: str) -> None:
    with pytest.raises(SpecError, match=message):
        OpenApiDocument(spec, spec_url=None)


def test_versions_written_as_numbers() -> None:
    assert OpenApiDocument({"swagger": 2.0}, spec_url=None).version == "2.0"
    assert OpenApiDocument({"swagger": 2}, spec_url=None).version == "2.0"
    assert OpenApiDocument({"openapi": JsonFloat("3.0")}, spec_url=None).version == "3.0"
    untitled = OpenApiDocument({"openapi": "3.0.0", "info": {"version": 2}}, spec_url=None)
    assert (untitled.title, untitled.api_version) == (None, "2")
    assert OpenApiDocument({"openapi": "3.0.0"}, spec_url=None).api_version is None


@pytest.mark.parametrize(
    ("spec", "spec_url", "expected"),
    [
        ({}, "https://api.example.com/docs/openapi.json", ("https://api.example.com", "")),
        ({"servers": []}, "http://localhost:8000/openapi.json", ("http://localhost:8000", "")),
        (
            {"servers": [{"url": "v1"}]},
            "https://h.example.com/docs/openapi.json",
            ("https://h.example.com", "/docs/v1"),
        ),
        (
            {"servers": [{"url": "//cdn.example.com/api/"}]},
            None,
            ("https://cdn.example.com", "/api"),
        ),
        (
            {
                "servers": [
                    {
                        "url": "https://{env}.example.com:{port}/v{major}",
                        "variables": {
                            "env": {"enum": ["prod", "dev"]},
                            "port": {"default": 8443},
                            "major": {"default": "2"},
                        },
                    }
                ]
            },
            None,
            ("https://prod.example.com:8443", "/v2"),
        ),
        (
            {"servers": [{"url": "HTTPS://API.Example.com/./"}]},
            None,
            ("https://api.example.com", ""),
        ),
        (
            {"servers": ["junk", {"url": "https://x.example.com"}]},
            None,
            ("https://x.example.com", ""),
        ),
    ],
)
def test_openapi3_real_target(
    spec: dict[str, JsonValue], spec_url: str | None, expected: tuple[str, str]
) -> None:
    target = inline(spec, spec_url).real_target()
    assert (target.base_url, target.base_path) == expected


@pytest.mark.parametrize(
    ("spec", "spec_url", "expected"),
    [
        (
            {"host": "api.example.com", "basePath": "/v1/", "schemes": ["http", "https"]},
            None,
            ("https://api.example.com", "/v1"),
        ),
        (
            {"host": "API.example.com:8080", "schemes": ["http"]},
            None,
            ("http://api.example.com:8080", ""),
        ),
        (
            {"basePath": "/api"},
            "http://localhost:5000/swagger.json",
            ("http://localhost:5000", "/api"),
        ),
        ({"host": "api.example.com"}, None, ("https://api.example.com", "")),
        (
            {"host": "api.example.com"},
            "http://docs.example.com/s.json",
            ("http://api.example.com", ""),
        ),
    ],
)
def test_swagger2_real_target(
    spec: dict[str, JsonValue], spec_url: str | None, expected: tuple[str, str]
) -> None:
    target = OpenApiDocument({"swagger": "2.0", **spec}, spec_url=spec_url).real_target()
    assert (target.base_url, target.base_path) == expected


def test_real_target_needs_an_absolute_location() -> None:
    relative = inline({"servers": [{"url": "/api/v3"}]}, "file:///tmp/openapi.json")
    assert relative.base_path == "/api/v3"
    with pytest.raises(SpecError, match="relative location"):
        relative.real_target()
    bare = inline({"servers": [{"url": "v2"}]})
    assert bare.base_path == "/v2"
    with pytest.raises(SpecError, match="provide the API's base URL"):
        OpenApiDocument({"swagger": "2.0", "basePath": "/v1"}, spec_url=None).real_target()
    no_default = inline({"servers": [{"url": "https://{tenant}.example.com"}]})
    with pytest.raises(SpecError, match=r"uses \{tenant\} but the spec gives it no default"):
        no_default.real_target()


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def test_resolve_edge_cases() -> None:
    doc = inline(
        {
            "paths": {"/things/{id}": {"get": {"summary": "by id"}}},
            "components": {
                "schemas": {
                    "A": {"type": "object", "description": "original"},
                    "List": [{"type": "string"}],
                    "Self": {"$ref": "#/components/schemas/Self"},
                }
            },
        }
    )
    assert doc.resolve({"$ref": "#/components/schemas/A", "description": "override"}) == {
        "type": "object",
        "description": "override",
    }
    assert doc.resolve({"$ref": "#/components/schemas/List/0"}) == {"type": "string"}
    assert doc.resolve({"$ref": "#/components/schemas/List", "description": "x"}) == [
        {"type": "string"}
    ]
    assert doc.resolve({"$ref": "#/paths/~1things~1%7Bid%7D/get/summary"}) == "by id"
    assert doc.resolve({"$ref": "#/components/schemas/Self"}) == {
        "$ref": "#/components/schemas/Self"
    }
    assert dig(doc.resolve({"$ref": "#"}), "info", "title") == "Inline"
    for dangling in ("#/components/schemas/Missing", "#Anchor", "#/components/schemas/List/9"):
        assert doc.resolve({"$ref": dangling}) == {"$ref": dangling}
        assert f"$ref {dangling!r} points at nothing; left as-is" in doc.notes
    assert doc.resolve([1, "x", None, {"$ref": 5}]) == [1, "x", None, {"$ref": 5}]

    notes: list[str] = []
    assert doc.resolve({"$ref": "other.yaml#/A"}, notes=notes) == {"$ref": "other.yaml#/A"}
    assert notes == ["external $ref 'other.yaml#/A' is not resolved"]
    assert "external $ref 'other.yaml#/A' is not resolved" not in doc.notes

    resolved = doc.resolve({"$ref": "#/components/schemas/A"})
    assert isinstance(resolved, dict)
    resolved["type"] = "changed"  # a copy: the document is untouched
    assert dig(doc.spec, "components", "schemas", "A", "type") == "object"


def test_ref_expansion_has_a_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    schemas: dict[str, JsonValue] = {
        f"S{level}": {
            "properties": {
                "left": {"$ref": f"#/components/schemas/S{level + 1}"},
                "right": {"$ref": f"#/components/schemas/S{level + 1}"},
            }
        }
        for level in range(40)
    }
    schemas["S40"] = {"type": "string"}
    doc = inline({"components": {"schemas": schemas}})
    resolved = doc.resolve({"$ref": "#/components/schemas/S0"})  # 2**40 paths: must stop
    assert "stopped expanding $refs after 1000 (huge schema)" in doc.notes
    assert "$ref" in str(resolved)

    monkeypatch.setattr(openapi, "MAX_REF_EXPANSIONS", 2)
    small = inline({"components": {"schemas": schemas}})
    chain = small.resolve({"$ref": "#/components/schemas/S0"})
    assert dig(chain, "properties", "left", "properties", "left") == {
        "$ref": "#/components/schemas/S2"
    }


# ---------------------------------------------------------------------------
# Operations and endpoint edge cases
# ---------------------------------------------------------------------------


def test_operations_resolve_path_item_refs_and_skip_the_rest() -> None:
    doc = inline(
        {
            "paths": {
                "/a": {"$ref": "#/components/pathItems/A"},
                "/b": {
                    "summary": "B",
                    "query": {"responses": {}},
                    "x-internal": True,
                    "get": {"responses": {}},
                    "post": "not an operation",
                },
                "/c": "not a path item",
                "/d": {"$ref": "#/components/pathItems/Missing"},
            },
            "components": {"pathItems": {"A": {"get": {"responses": {}}}}},
            "webhooks": {"newThing": {"post": {}}},
        }
    )
    assert [(op.method.value, op.path) for op in doc.operations()] == [("GET", "/a"), ("GET", "/b")]
    assert "/b: unsupported operation 'query' skipped" in doc.notes
    assert "/b: unsupported operation 'post' skipped" in doc.notes
    assert "webhooks (calls the API makes; not faked): newThing" in doc.notes


def test_openapi3_parameter_body_and_response_edge_cases() -> None:
    doc = inline(
        {
            "paths": {
                "/things/{id}": {
                    "servers": [{"url": "https://files.example.com"}],
                    "post": {
                        "description": "\n\n  Second paragraph first line.\nMore.",
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": ["string", "null"]}},
                            {
                                "name": "filter",
                                "in": "query",
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            },
                            {"name": "session", "in": "cookie", "schema": {"type": "string"}},
                            {"name": "Authorization", "in": "header"},
                            {"name": "x", "in": "body"},
                            {"in": "query"},
                            "garbage",
                            {"$ref": "#/components/parameters/Missing"},
                        ],
                        "requestBody": {"content": {}},
                        "responses": {
                            "200": {"description": "ok", "content": {"text/plain": {}}},
                            "999": {"description": "bogus"},
                            "2XX": {},
                        },
                        "callbacks": {"onEvent": {}},
                        "deprecated": "yes",
                    },
                }
            }
        }
    )
    (post,) = endpoints_from_spec(doc)
    assert post.summary == "Second paragraph first line."
    assert [(p.name, p.location, p.required) for p in post.params] == [
        ("id", "path", True),
        ("filter", "query", False),
        ("session", "cookie", False),
    ]
    assert post.params[0].param_schema == {"type": ["string", "null"]}
    assert post.params[1].param_schema == {"type": "object"}
    assert post.request_body == RequestBodyModel(content_types=["application/json"])
    assert [(r.status, r.content_type, r.documented_schema) for r in post.responses] == [
        (200, "text/plain", None)
    ]
    assert not post.deprecated
    assert post.notes == [
        "a parameter without 'name' and 'in' was skipped",
        "a parameter without 'name' and 'in' was skipped",
        "$ref '#/components/parameters/Missing' points at nothing; left as-is",
        (
            "header parameter 'Authorization' ignored (OpenAPI leaves it to content negotiation "
            "and auth)"
        ),
        "parameter 'x' in unsupported location 'body' skipped",
        "documented '999' response: bogus (not a single status)",
        "documented '2XX' response (not a single status)",
        "operation-specific servers: https://files.example.com",
        "callbacks (not faked): onEvent",
    ]


def test_parameter_defaults_inside_all_of() -> None:
    doc = inline(
        {
            "paths": {
                "/a": {
                    "get": {
                        "parameters": [
                            {
                                "name": "size",
                                "in": "query",
                                "schema": {"allOf": [{"type": "integer"}, {"default": 5}]},
                            },
                            {
                                "name": "none",
                                "in": "query",
                                "schema": {"allOf": [{"type": "integer"}]},
                            },
                        ]
                    }
                }
            }
        }
    )
    (get,) = endpoints_from_spec(doc)
    assert [p.default for p in get.params] == [5, None]


def test_swagger2_parameters_bodies_and_responses() -> None:
    doc = OpenApiDocument(
        {
            "swagger": "2.0",
            "host": "api.example.com",
            "consumes": ["application/json"],
            "produces": ["application/xml", "application/json"],
            "paths": {
                "/items/{ids}": {
                    "parameters": [
                        {"name": "ids", "in": "path", "type": "array", "items": {"type": "integer"}}
                    ],
                    "get": {
                        "parameters": [
                            {
                                "name": "tags",
                                "in": "query",
                                "type": "array",
                                "collectionFormat": "multi",
                                "items": {"type": "string", "enum": ["a", "b"]},
                            },
                            {
                                "name": "sizes",
                                "in": "query",
                                "type": "array",
                                "collectionFormat": "pipes",
                                "items": {"type": "Double"},
                            },
                            {
                                "name": "names",
                                "in": "query",
                                "type": "array",
                                "collectionFormat": "ssv",
                                "items": {"type": "string"},
                            },
                            {
                                "name": "cols",
                                "in": "query",
                                "type": "array",
                                "collectionFormat": "tsv",
                                "items": {"type": "string"},
                            },
                            {"name": "X-Ids", "in": "header", "type": "array", "items": {}},
                            {"name": "page", "in": "query", "type": "integer", "minimum": 1},
                            {"name": "flag", "in": "query", "type": "boolean", "style": "form"},
                            {"name": "fancy", "in": "query", "type": "uuid"},
                        ],
                        "responses": {
                            "200": {
                                "description": "ok",
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/definitions/Item"},
                                },
                            },
                            "204": {"description": "nothing"},
                            "404": {
                                "description": "missing",
                                "schema": {"$ref": "#/definitions/E"},
                            },
                        },
                    },
                    "put": {
                        "consumes": ["application/vnd.api+json"],
                        "parameters": [
                            {
                                "name": "body",
                                "in": "body",
                                "required": True,
                                "schema": {"$ref": "#/definitions/Item"},
                            }
                        ],
                        "responses": {"200": {"description": "ok"}},
                    },
                    "post": {
                        "parameters": [
                            {"name": "file", "in": "formData", "type": "file", "required": True},
                            {"name": "note", "in": "formData", "type": "string"},
                            {"name": "weird", "in": "formData"},
                        ],
                        "responses": {"201": {"description": "created", "schema": {}}},
                    },
                    "patch": {
                        "consumes": ["multipart/form-data", "application/json"],
                        "parameters": [{"name": "note", "in": "formData", "type": "string"}],
                        "responses": {},
                    },
                }
            },
            "definitions": {"Item": {"type": "object"}, "E": {"type": "object"}},
        },
        spec_url=None,
    )
    get, put, post, patch = endpoints_from_spec(doc)
    assert [(p.name, p.style, p.explode) for p in get.params] == [
        ("ids", "simple", False),
        ("tags", "form", True),
        ("sizes", "pipeDelimited", False),
        ("names", "spaceDelimited", False),
        ("cols", None, None),
        ("X-Ids", "simple", False),
        ("page", None, None),
        ("flag", "form", None),
        ("fancy", None, None),
    ]
    assert get.params[0].param_schema == {"type": "array", "items": {"type": "integer"}}
    assert get.params[2].param_schema == {"type": "array", "items": {"type": "number"}}
    assert get.params[5].param_schema == {"type": "array", "items": None}
    assert get.params[8].param_schema == {"type": "uuid"}  # unknown types are kept as written
    assert get.notes == [
        "parameter 'sizes' declares non-standard type 'Double' (read as 'number')",
        "parameter 'cols': collectionFormat 'tsv' has no OpenAPI 3 style",
    ]
    assert [(r.status, r.content_type) for r in get.responses] == [
        (200, "application/json"),
        (204, None),
        (404, "application/json"),
    ]
    assert get.responses[0].documented_schema == {"type": "array", "items": {"type": "object"}}

    assert put.request_body == RequestBodyModel(
        content_types=["application/vnd.api+json"], body_schema={"type": "object"}, required=True
    )
    assert put.responses[0].content_type == "application/json"

    assert post.request_body == RequestBodyModel(
        content_types=["multipart/form-data"],
        body_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "format": "binary"},
                "note": {"type": "string"},
                "weird": {},
            },
            "required": ["file"],
        },
        required=True,
    )
    assert patch.request_body is not None
    assert patch.request_body.content_types == ["multipart/form-data"]
    assert not patch.request_body.required
    assert patch.success_status == 200


def test_swagger2_without_produces_assumes_json_for_schemas() -> None:
    doc = OpenApiDocument(
        {
            "swagger": "2.0",
            "paths": {
                "/a": {
                    "get": {
                        "responses": {
                            "200": {"description": "ok", "schema": {"type": "object"}},
                            "202": {"description": "accepted"},
                        }
                    }
                }
            },
        },
        spec_url="https://api.example.com/swagger.json",
    )
    (get,) = endpoints_from_spec(doc)
    assert [(r.status, r.content_type) for r in get.responses] == [
        (200, "application/json"),
        (202, None),
    ]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_swagger2_auth() -> None:
    doc = OpenApiDocument(
        {
            "swagger": "2.0",
            "securityDefinitions": {
                "basic": {"type": "basic"},
                "key": {"type": "apiKey", "in": "query", "name": "api_key"},
                "oauth": {
                    "type": "oauth2",
                    "flow": "implicit",
                    "authorizationUrl": "https://auth.example.com",
                    "scopes": {"read": "Read", "write": "Write"},
                },
                "broken": {"type": "apiKey", "in": "body", "name": "x"},
                "nameless": {"type": "apiKey", "in": "header"},
            },
            "security": [{"basic": []}],
            "paths": {
                "/open": {"get": {"security": []}},
                "/optional": {"get": {"security": [{}, {"key": []}]}},
                "/closed": {"get": {}},
                "/explicit": {"get": {"security": [{"oauth": ["read"]}]}},
            },
        },
        spec_url="https://api.example.com/swagger.json",
    )
    auth = auth_from_spec(doc)
    assert auth is not None
    assert [(s.kind, s.location, s.name, s.prefix) for s in auth.schemes] == [
        ("basic", None, None, "Basic "),
        ("api_key", "query", "api_key", None),
        ("oauth2", None, None, "Bearer "),
    ]
    assert auth.schemes[2].oauth is not None
    assert auth.schemes[2].oauth.scopes == ["read", "write"]
    assert (auth.required_for, auth.exempt) == (["*"], ["GET /open", "GET /optional"])


def test_openapi3_auth_variants() -> None:
    doc = inline(
        {
            "components": {
                "securitySchemes": {
                    "basic": {"type": "http", "scheme": "Basic"},
                    "bearer": {"type": "http", "scheme": "bearer"},
                    "bearer2": {"type": "http", "scheme": "bearer"},
                    "digest": {"type": "http", "scheme": "digest"},
                    "oidc": {"type": "openIdConnect", "openIdConnectUrl": "https://x.example.com"},
                    "mtls": {"type": "mutualTLS"},
                    "alias": {"$ref": "#/components/securitySchemes/basic"},
                    "junk": "not a scheme",
                }
            },
            "security": [{}],
            "paths": {
                "/a": {"get": {"security": [{"bearer": []}]}},
                "/b": {"get": {"security": []}},
                "/c": {"get": {"security": "bogus"}},
            },
        }
    )
    auth = auth_from_spec(doc)
    assert auth is not None
    assert [s.kind for s in auth.schemes] == ["basic", "bearer", "oauth2"]
    assert auth.schemes[2].oauth is not None
    assert auth.schemes[2].oauth.scopes == []
    assert (auth.required_for, auth.exempt) == (["GET /a"], [])


def test_no_usable_auth() -> None:
    assert auth_from_spec(inline({})) is None
    unusable: dict[str, JsonValue] = {
        "components": {"securitySchemes": {"m": {"type": "mutualTLS"}}}
    }
    assert auth_from_spec(inline(unusable)) is None
    assert auth_from_spec(inline({"components": {"securitySchemes": ["not", "a", "map"]}})) is None


# ---------------------------------------------------------------------------
# Role suggestions
# ---------------------------------------------------------------------------

ARRAY: JsonValue = {"type": "array", "items": {"type": "object"}}
OBJECT: JsonValue = {"type": "object", "properties": {"count": {"type": "integer"}}}


@pytest.mark.parametrize(
    ("subject", "others", "expected"),
    [
        (
            endpoint("GET", "/things/{id}/events", responses=[(200, "text/event-stream", None)]),
            [],
            ("sse", None),
        ),
        (endpoint("POST", "/files", request_types=["multipart/form-data"]), [], ("upload", None)),
        (
            endpoint("PUT", "/objects/{key}", request_types=["application/octet-stream"]),
            [],
            ("upload", None),
        ),
        (
            endpoint("GET", "/files/{id}/content", responses=[(200, "application/pdf", None)]),
            [],
            ("download", None),
        ),
        (
            endpoint("GET", "/avatars/{id}", responses=[(201, "image/png; q=1", None)]),
            [],
            ("download", None),
        ),
        (
            endpoint(
                "POST",
                "/exports",
                responses=[(201, "application/json", None), (200, "application/zip", None)],
                success_status=201,
            ),
            [],
            ("create", "exports"),
        ),
        (
            endpoint("GET", "/reports", responses=[(404, "application/pdf", None)]),
            [],
            ("list", "reports"),
        ),
        (endpoint("GET", "/things/search"), [], ("search", "things")),
        (endpoint("GET", "/pet/findByStatus"), [], ("search", "pet")),
        (endpoint("GET", "/things", params=[("q", "query")]), [], ("search", "things")),
        (endpoint("GET", "/things", params=[("q", "header")]), [], ("list", "things")),
        (endpoint("GET", "/searches"), [], ("list", "searches")),
        (endpoint("GET", "/things/{id}"), [], ("get", "things")),
        (endpoint("GET", "/things"), [], ("list", "things")),
        (endpoint("GET", "/things", responses=[(200, None, {})]), [], ("list", "things")),
        (endpoint("GET", "/things", responses=[(200, None, ARRAY)]), [], ("list", "things")),
        (
            endpoint("GET", "/things", responses=[(200, None, {"type": ["array", "null"]})]),
            [],
            ("list", "things"),
        ),
        (
            endpoint(
                "GET",
                "/things",
                responses=[(200, None, {"type": "object", "properties": {"data": ARRAY}})],
            ),
            [],
            ("list", "things"),
        ),
        (
            endpoint(
                "GET",
                "/things",
                responses=[(200, None, {"allOf": [OBJECT, {"properties": {"items": ARRAY}}]})],
            ),
            [],
            ("list", "things"),
        ),
        (
            endpoint("GET", "/things", responses=[(200, None, {"allOf": [{"items": {}}]})]),
            [],
            ("list", "things"),
        ),
        (endpoint("GET", "/stats", responses=[(200, None, OBJECT)]), [], ("replay", None)),
        (
            endpoint("GET", "/things", responses=[(200, None, OBJECT)]),
            [endpoint("POST", "/things/{id}"), endpoint("GET", "/things/{id}")],
            ("list", "things"),
        ),
        (
            endpoint("GET", "/things", responses=[(200, None, OBJECT)]),
            [endpoint("GET", "/things/{id}/parts"), endpoint("GET", "/thingsx/{id}")],
            ("replay", None),
        ),
        (endpoint("POST", "/things"), [], ("create", "things")),
        (endpoint("POST", "/things/{id}"), [], ("update", "things")),
        (endpoint("PUT", "/things/{id}"), [], ("replace", "things")),
        (endpoint("PATCH", "/things/{id}"), [], ("update", "things")),
        (endpoint("DELETE", "/things/{id}"), [], ("delete", "things")),
        (endpoint("HEAD", "/things"), [], ("replay", None)),
        (endpoint("GET", "/v1/{date}"), [], ("replay", None)),
        (endpoint("GET", "/api/v2/user-groups"), [], ("list", "user_groups")),
        (endpoint("POST", "/user/createWithList"), [], ("create", "user")),
        (
            endpoint("GET", "/api/v3/pet/{petId}", spec_path="/pet/{petId}"),
            [],
            ("get", "pet"),
        ),
    ],
)
def test_suggest_role(
    subject: EndpointModel,
    others: list[EndpointModel],
    expected: tuple[EndpointRole, str | None],
) -> None:
    assert suggest_role(subject, [subject, *others]) == expected


def test_suggest_role_on_real_specs() -> None:
    petstore = endpoints_from_spec(load("petstore3-openapi.json"))
    roles = {e.id: suggest_role(e, petstore) for e in petstore}
    assert roles["GET /api/v3/pet/findByStatus"] == ("search", "pet")
    assert roles["POST /api/v3/pet/{petId}/uploadImage"] == ("upload", None)
    assert roles["GET /api/v3/store/inventory"] == ("replay", None)
    assert roles["POST /api/v3/store/order"] == ("create", "order")
    refs = endpoints_from_spec(load("refs-openapi.yaml", fetched=False))
    assert [suggest_role(e, refs) for e in refs] == [
        ("list", "things"),
        ("create", "things"),
        ("get", "things"),
        ("delete", "things"),
        ("upload", None),
        ("sse", None),
        ("replay", None),
    ]
    httpbin = endpoints_from_spec(load("httpbin-spec.json"))
    by = {e.id: suggest_role(e, httpbin) for e in httpbin}
    assert by["GET /image/png"] == ("download", None)
    assert by["GET /get"] == ("replay", None)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summarize_petstore3() -> None:
    doc = load("petstore3-openapi.json")
    text = summarize(doc, endpoints_from_spec(doc))
    lines = text.splitlines()
    assert lines[:5] == [
        "# Swagger Petstore - OpenAPI 3.0 (version 1.0.27)",
        "",
        "- Spec: OpenAPI 3.0.4",
        "- Base URL: https://petstore3.swagger.io/api/v3",
        (
            "- Auth: petstore_auth (oauth2; scopes: write:pets, read:pets); "
            "api_key (API key in header 'api_key')"
        ),
    ]
    assert "## Endpoints (19)" in lines
    assert (
        "- `GET /api/v3/pet/findByStatus` — Finds Pets by status. "
        "[status*: query string {available|pending|sold} = available] -> 200, 400, default"
    ) in lines
    assert (
        "- `PUT /api/v3/pet` — Update an existing pet. [body*: application/json (+2 more)] "
        "-> 200, 400, 404, 422, default"
    ) in lines
    assert "- `GET /api/v3/pet/{petId}` — Find pet by ID. [petId*: path integer(int64)] -> " in (
        text
    )
    assert text.endswith("\n")


def test_summarize_other_shapes() -> None:
    refs = load("refs-openapi.yaml", fetched=False)
    text = summarize(refs, endpoints_from_spec(refs))
    assert "- Base URL: https://eu.api.example.com/v2" in text
    assert (
        "- Auth: ApiKeyAuth (API key in header 'X-API-Key'); BearerAuth (HTTP bearer, JWT); "
        "OAuth (oauth2; scopes: things:read, things:write, admin) — required unless an "
        "endpoint opts out"
    ) in text
    assert "- `DELETE /v2/things/{thingId}` — Delete a thing" in text
    assert "-> 204 (deprecated)" in text
    assert "-> 201, 4XX" in text

    timeapi = load("timeapi-swagger.json")
    lines = summarize(timeapi, endpoints_from_spec(timeapi)).splitlines()
    unix_ns = next(line for line in lines if "/unix_ns" in line)
    assert "the last 3 digits will often be zero. -> 200" in unix_ns  # one line per endpoint

    frankfurter = load("frankfurter-openapi.json")
    text = summarize(frankfurter, endpoints_from_spec(frankfurter))
    assert "base: query string {AUD|BGN|BRL|CAD|CHF|CNY|…} = EUR" in text

    relative = load("petstore3-openapi.json", fetched=False)
    assert "- Base URL: unknown host (relative server; base path /api/v3)" in summarize(
        relative, []
    )


def test_summarize_rendering_details() -> None:
    doc = inline(
        {
            "components": {
                "securitySchemes": {
                    "oidc": {"type": "openIdConnect"},
                    "basic": {"type": "http", "scheme": "basic"},
                    "mtls": {"type": "mutualTLS"},
                }
            },
            "paths": {
                "/a/{id}": {
                    "get": {
                        "summary": "A",
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"$ref": "#/x/Cyclic"}},
                            {"name": "n", "in": "query", "schema": {"type": ["string", "null"]}},
                            {
                                "name": "v",
                                "in": "query",
                                "schema": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
                            },
                            {"name": "w", "in": "query", "schema": {"type": "array"}},
                            {"name": "z", "in": "query", "schema": {"not": {}}},
                            {"name": "big", "in": "query", "schema": {"enum": list("abcdefgh")}},
                        ],
                        "requestBody": {
                            "content": {"application/x-www-form-urlencoded": {}},
                            "required": True,
                        },
                    }
                }
            },
        }
    )
    endpoints = endpoints_from_spec(doc)
    extra = endpoint("POST", "/b", responses=[(201, None, None)])
    bare = endpoint("GET", "/c")
    text = summarize(doc, [*endpoints, extra, bare])
    assert "- Auth: oidc (openIdConnect); basic (HTTP basic); mtls (mutualTLS)\n" in text
    assert (
        "- `GET /a/{id}` — A [id*: path Cyclic; n: query string|null; v: query integer | string; "
        "w: query any[]; z: query; big: query {a|b|c|d|e|f|…}; "
        "body*: application/x-www-form-urlencoded] -> no documented responses"
    ) in text
    assert "- `POST /b` -> 201\n" in text
    assert "- `GET /c` -> no documented responses\n" in text
    untitled = OpenApiDocument({"openapi": "3.0.0"}, spec_url="https://x.example.com/o.json")
    assert summarize(untitled, []).splitlines()[:5] == [
        "# Untitled API",
        "",
        "- Spec: OpenAPI 3.0.0",
        "- Base URL: https://x.example.com",
        "- Auth: none documented",
    ]


def test_parameter_descriptions_are_kept() -> None:
    endpoints = {e.id: e for e in endpoints_from_spec(load("petstore3-openapi.json"))}
    pet_id = endpoints["GET /api/v3/pet/{petId}"].params[0]
    assert (pet_id.name, pet_id.description) == ("petId", "ID of pet to return")
