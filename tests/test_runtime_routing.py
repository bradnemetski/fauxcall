from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pytest

from fauxcall.model.api import ApiModel
from fauxcall.runtime.routing import RoutingQuirks, path_value, route_identifier

if TYPE_CHECKING:
    from starlette.types import Message, Scope

ADMIN = "/__fauxcall"


def api(
    paths: list[str],
    *,
    case_insensitive: bool = True,
    trailing: str = "strict",
    spec_paths: tuple[str, ...] = (),
    mcp_path: str | None = None,
) -> ApiModel:
    document: dict[str, Any] = {
        "name": "books",
        "title": "Books",
        "source": {
            "kind": "openapi",
            "url": "https://api.test/spec",
            "fetched_at": "2026-10-10T00:00:00Z",
        },
        "real": {"base_url": "https://api.test"},
        "routing": {
            "case_insensitive": case_insensitive,
            "trailing_slash": trailing,
            "spec_paths": list(spec_paths),
        },
        "endpoints": [{"id": f"GET {path}", "method": "GET", "path": path} for path in paths],
    }
    if mcp_path is not None:
        document["mcp"] = {"transport": {"path": mcp_path}}
    return ApiModel.model_validate(document)


def quirks(paths: list[str], **options: Any) -> RoutingQuirks:
    return RoutingQuirks(None, model=api(paths, **options), admin_prefix=ADMIN)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("petId", "petId"),
        ("pet-id", "pet_id"),
        ("a.b c", "a_b_c"),
        ("1st", "p_1st"),
        ("", "p_"),
        ("für", "f_r"),  # Starlette only accepts ASCII identifiers in route templates
        ("_x9", "_x9"),
    ],
)
def test_route_identifier(name: str, expected: str) -> None:
    assert route_identifier(name) == expected


def test_path_value_by_spec_name_or_routed_identifier() -> None:
    params = {"pet_id": "42", "petId": 7}
    assert path_value(params, "petId") == 7
    assert path_value(params, "pet-id") == "42"
    assert path_value(MappingProxyType(params), "pet-id") == "42"
    assert path_value(params, "owner") is None


def test_literal_segments_are_respelled_and_parameters_kept() -> None:
    routes = quirks(["/api/v1/Books/{id}", "/api/v1/Authors"])
    assert routes.canonical("/API/V1/books/AbC-7") == "/api/v1/Books/AbC-7"
    assert routes.canonical("/api/v1/authors") == "/api/v1/Authors"
    assert routes.canonical("/api/v1/Books/7") == "/api/v1/Books/7"


def test_literal_routes_beat_parameter_routes() -> None:
    routes = quirks(["/api/Books/{id}", "/api/books/special"])
    assert routes.canonical("/api/books/special") == "/api/books/special"
    assert routes.canonical("/API/BOOKS/SPECIAL") == "/api/books/special"
    assert routes.canonical("/api/books/7") == "/api/Books/7"


def test_an_exact_match_is_never_respelled() -> None:
    routes = quirks(["/a/B", "/a/b"])
    assert routes.canonical("/a/b") == "/a/b"
    assert routes.canonical("/a/B") == "/a/B"
    assert routes.canonical("/A/b") == "/a/B"  # same specificity: the first in path order


def test_non_matching_paths_and_admin_paths_are_untouched() -> None:
    routes = quirks(["/api/Books/{id}"], trailing="ignore")
    assert routes.canonical("/api/Authors/1") == "/api/Authors/1"
    assert routes.canonical("/api/books/1/reviews") == "/api/books/1/reviews"
    assert routes.canonical(f"{ADMIN}/Journal/") == f"{ADMIN}/Journal/"
    assert routes.canonical("/") == "/"


def test_trailing_slashes() -> None:
    ignore = quirks(["/pets", "/"], trailing="ignore", case_insensitive=False)
    assert ignore.canonical("/pets/") == "/pets"
    assert ignore.canonical("/pets//") == "/pets"
    assert ignore.canonical("/") == "/"
    assert ignore.canonical("/PETS/") == "/PETS"
    ignore_ci = quirks(["/pets"], trailing="ignore")
    assert ignore_ci.canonical("/PETS/") == "/pets"
    strict_ci = quirks(["/pets"], trailing="strict")
    assert strict_ci.canonical("/PETS/") == "/pets/"
    plain = quirks(["/pets"], trailing="strict", case_insensitive=False)
    assert plain.canonical("/PETS/") == "/PETS/"


def test_spec_and_mcp_paths_are_routes_too() -> None:
    routes = quirks(["/api/Books"], spec_paths=("/openapi.json",), mcp_path="/Mcp")
    assert routes.canonical("/OpenAPI.json") == "/openapi.json"
    assert routes.canonical("/mcp") == "/Mcp"


async def receive() -> Message:
    return {"type": "http.request"}


async def send(message: Message) -> None:
    del message


async def test_middleware_rewrites_http_and_websocket_scopes() -> None:
    seen: list[Scope] = []

    async def app(scope: Scope, _receive: Any, _send: Any) -> None:
        seen.append(scope)

    middleware = RoutingQuirks(app, model=api(["/api/Books/{title}"]), admin_prefix=ADMIN)
    http: Scope = {"type": "http", "path": "/API/books/a b%", "raw_path": b"/API/books/a%20b%25"}
    await middleware(http, receive, send)
    assert seen[-1]["path"] == "/api/Books/a b%"
    assert seen[-1]["raw_path"] == b"/api/Books/a%20b%25"
    assert http["path"] == "/API/books/a b%"  # the incoming scope is not mutated
    websocket: Scope = {"type": "websocket", "path": "/api/BOOKS/é", "raw_path": b""}
    await middleware(websocket, receive, send)
    assert seen[-1]["path"] == "/api/Books/é"
    assert seen[-1]["raw_path"] == b"/api/Books/%C3%A9"
    unchanged: Scope = {"type": "http", "path": "/api/Books/x", "raw_path": b"/api/Books/x"}
    await middleware(unchanged, receive, send)
    assert seen[-1] is unchanged
    lifespan: Scope = {"type": "lifespan"}
    await middleware(lifespan, receive, send)
    assert seen[-1] is lifespan
