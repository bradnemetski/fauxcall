"""Custom behavior of this fake: yours to edit, ``fauxcall scaffold`` never overwrites it.

FakeRESTApi (an ASP.NET Core demo) generates its data from fixed rules and never stores writes.
What needs code here (everything else is served by the engine from ``data/model.json``):

- **Item lookups** bind the id like ASP.NET's ``Int32Converter`` (``+5``, `` 1``, ``0x10``), see
  :mod:`fauxcall_fakerestapi.aspnet`.
- **Authors** are regenerated on every request: each book gets 2-4 authors, numbered in order
  (recorded totals 588-608 for 200 books), so ids and ``idBook`` differ between calls.
- **Writes** (POST/PUT) bind the body with System.Text.Json's rules and echo the bound model;
  DELETE answers an empty 200. Nothing is persisted (read-backs after writes show the seed data),
  see :mod:`fauxcall_fakerestapi.stj`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fauxcall_fakerestapi import aspnet, stj

if TYPE_CHECKING:
    import random
    from collections.abc import Awaitable, Callable

    from fastapi import Response, WebSocket
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

API: Final = "/api/v1"
SUCCESS_TYPE: Final = "application/json; charset=utf-8; v=1.0"
VERSION_HEADER: Final = {"api-supported-versions": "1.0"}
AUTHORS_PER_BOOK: Final = (2, 4)

DTOS: Final[dict[str, stj.Dto]] = {
    "Activities": stj.Dto(
        "FakeRestAPI.Models.Activity",
        (("id", "int"), ("title", "string"), ("dueDate", "datetime"), ("completed", "bool")),
    ),
    "Authors": stj.Dto(
        "FakeRestAPI.Models.Author",
        (("id", "int"), ("idBook", "int"), ("firstName", "string"), ("lastName", "string")),
    ),
    "Books": stj.Dto(
        "FakeRestAPI.Models.Book",
        (
            ("id", "int"),
            ("title", "string"),
            ("description", "string"),
            ("pageCount", "int"),
            ("excerpt", "string"),
            ("publishDate", "datetime"),
        ),
    ),
    "CoverPhotos": stj.Dto(
        "FakeRestAPI.Models.CoverPhoto", (("id", "int"), ("idBook", "int"), ("url", "uri"))
    ),
    "Users": stj.Dto(
        "FakeRestAPI.Models.User", (("id", "int"), ("userName", "string"), ("password", "string"))
    ),
}
COLLECTIONS: Final = {
    "Activities": "activities",
    "Books": "books",
    "CoverPhotos": "coverphotos",
    "Users": "users",
}


def validation_error(ctx: RequestContext, errors: dict[str, list[str]]) -> Response:
    """The real API's 400 (``ValidationProblemDetails``) with these ``errors``."""
    return ctx.error("invalid_body", template="validation", values={"errors": errors})  # type: ignore[dict-item]


def route_id(ctx: RequestContext, name: str) -> tuple[int | None, dict[str, list[str]]]:
    """Bind a route parameter; returns (value, model-state errors)."""
    raw = ctx.path_param(name) or ""
    value = aspnet.bind_int32(raw)
    return value, ({} if value is not None else {name: [aspnet.invalid_value(raw)]})


def ok(ctx: RequestContext, body: JsonValue) -> Response:
    """A 200 spelled like the real API's (content type and version header)."""
    return ctx.respond(body, status=200, content_type=SUCCESS_TYPE, headers=VERSION_HEADER)


# -- seed data lookups ---------------------------------------------------------------------------


def _get_item(resource: str) -> Callable[[RequestContext], Awaitable[Response]]:
    collection = COLLECTIONS[resource]

    async def get_item(ctx: RequestContext) -> Response:
        key, errors = route_id(ctx, "id")
        if key is None:
            return validation_error(ctx, errors)
        record = ctx.world.get(collection, key)
        if record is None:
            return ctx.error("not_found")
        return ok(ctx, ctx.render_record(record))

    return get_item


async def covers_by_book(ctx: RequestContext) -> Response:
    """``GET /CoverPhotos/books/covers/{idBook}``: the book's cover photos ([] when none)."""
    book, errors = route_id(ctx, "idBook")
    if book is None:
        return validation_error(ctx, errors)
    covers = [
        ctx.render_record(record)
        for record in ctx.world.list("coverphotos")
        if isinstance(record.record, dict) and record.record.get("idBook") == book
    ]
    return ok(ctx, covers)


# -- authors: regenerated on every request -----------------------------------------------------


def generate_authors(book_ids: list[int], rng: random.Random) -> list[JsonValue]:
    """One fresh set of authors, as the real API makes on each request."""
    authors: list[JsonValue] = []
    for book in book_ids:
        for _ in range(rng.randint(*AUTHORS_PER_BOOK)):
            number = len(authors) + 1
            authors.append(
                {
                    "id": number,
                    "idBook": book,
                    "firstName": f"First Name {number}",
                    "lastName": f"Last Name {number}",
                }
            )
    return authors


def _authors(ctx: RequestContext) -> list[JsonValue]:
    books: list[int] = []
    for record in ctx.world.list("books", copy=False):
        key = record.record.get("id") if isinstance(record.record, dict) else None
        if isinstance(key, int):
            books.append(key)
    return generate_authors(sorted(books), ctx.rng)


async def authors_list(ctx: RequestContext) -> Response:
    """``GET /Authors``."""
    return ok(ctx, _authors(ctx))


async def authors_get(ctx: RequestContext) -> Response:
    """``GET /Authors/{id}`` (404 when this request's set has fewer authors)."""
    key, errors = route_id(ctx, "id")
    if key is None:
        return validation_error(ctx, errors)
    found = [a for a in _authors(ctx) if isinstance(a, dict) and a["id"] == key]
    return ok(ctx, found[0]) if found else ctx.error("not_found")


async def authors_by_book(ctx: RequestContext) -> Response:
    """``GET /Authors/authors/books/{idBook}`` ([] for unknown books)."""
    book, errors = route_id(ctx, "idBook")
    if book is None:
        return validation_error(ctx, errors)
    return ok(ctx, [a for a in _authors(ctx) if isinstance(a, dict) and a["idBook"] == book])


# -- writes: bound, echoed, never stored --------------------------------------------------------


def _media_problem(ctx: RequestContext) -> Response | None:
    """415 for non-JSON bodies; an empty 500 for UTF-16/32 ones.

    Unsupported ``v=`` versions are refused earlier, for every method (``app.ApiVersioning``).
    """
    media, params = aspnet.media_type(ctx.header("content-type"))
    if not aspnet.accepts_json(media):
        return ctx.error("unsupported_media_type")
    if aspnet.unreadable_charset(params):
        return ctx.respond_bytes(b"", status=500)
    return None


def _write(resource: str, *, with_id: bool) -> Callable[[RequestContext], Awaitable[Response]]:
    dto = DTOS[resource]

    async def write_echo(ctx: RequestContext) -> Response:
        problem = _media_problem(ctx)
        if problem is not None:
            return problem
        errors: dict[str, list[str]] = {}
        values: dict[str, object] | None = None
        try:
            values = stj.bind(ctx.body_bytes, dto)
        except stj.BindingError as error:
            errors[error.key] = [error.message]
        if with_id:
            errors.update(route_id(ctx, "id")[1])
        if errors or values is None:
            return validation_error(ctx, errors)
        text = stj.serialize(values, dto)
        return ctx.respond_bytes(
            text.encode(), status=200, content_type=SUCCESS_TYPE, headers=VERSION_HEADER
        )

    return write_echo


async def delete_noop(ctx: RequestContext) -> Response:
    """``DELETE /<resource>/{id}``: 200, empty body, for any valid id; nothing is deleted."""
    key, errors = route_id(ctx, "id")
    if key is None:
        return validation_error(ctx, errors)
    return ctx.empty(200, headers=VERSION_HEADER)


for _resource in DTOS:
    if _resource in COLLECTIONS:
        HANDLERS[f"GET {API}/{_resource}/{{id}}"] = _get_item(_resource)
    HANDLERS[f"POST {API}/{_resource}"] = _write(_resource, with_id=False)
    HANDLERS[f"PUT {API}/{_resource}/{{id}}"] = _write(_resource, with_id=True)
    HANDLERS[f"DELETE {API}/{_resource}/{{id}}"] = delete_noop
HANDLERS[f"GET {API}/Authors"] = authors_list
HANDLERS[f"GET {API}/Authors/{{id}}"] = authors_get
HANDLERS[f"GET {API}/Authors/authors/books/{{idBook}}"] = authors_by_book
HANDLERS[f"GET {API}/CoverPhotos/books/covers/{{idBook}}"] = covers_by_book
