"""End-to-end read scenarios: identical assertions hold on the real API and on the fake.

Run against the fake (default), the real API (``--target=real``) or both (``--target=parity``).
Writes live in ``test_writes.py``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fauxcall.model.fields import IgnorePolicy
from fauxcall.testing import CompareOptions

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient

API = "/api/v1"
SUCCESS_TYPE = "application/json; charset=utf-8; v=1.0"
PROBLEM_TYPE = "application/problem+json; charset=utf-8"
TRACE_ID = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-00")
LOREM = "Lorem lorem lorem. Lorem lorem lorem. Lorem lorem lorem.\n"
RANDOM_AUTHORS = CompareOptions(values=False, lengths=False)
"""Authors are regenerated randomly on every request of the real API: compare shapes only."""

COUNTS = {"Activities": 30, "Books": 200, "CoverPhotos": 200, "Users": 10}


def parse_time(text: str) -> datetime:
    """A .NET round-trip timestamp (up to 7 fraction digits) as a datetime."""
    match = re.fullmatch(r"(.{19})(?:\.(\d{1,7}))?(Z|[+-]\d{2}:\d{2})?", text)
    assert match, text
    base, fraction, zone = match.groups()
    micro = (fraction or "0").ljust(7, "0")[:6]
    offset = "+00:00" if zone in {None, "Z"} else zone
    return datetime.fromisoformat(f"{base}.{micro}{offset}")


def assert_not_found(response: Any) -> None:
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_TYPE + "; v=1.0"
    assert response.headers["api-supported-versions"] == "1.0"
    body = response.json()
    assert body["type"] == "https://tools.ietf.org/html/rfc7231#section-6.5.4"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert TRACE_ID.fullmatch(body["traceId"])


def assert_invalid(response: Any, param: str, raw: str, message: str | None = None) -> None:
    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM_TYPE
    assert "api-supported-versions" not in response.headers
    body = response.json()
    assert body["title"] == "One or more validation errors occurred."
    assert body["errors"] == {param: [message or f"The value '{raw}' is not valid."]}
    assert TRACE_ID.fullmatch(body["traceId"])


# -- collections -------------------------------------------------------------------------------


@pytest.mark.parametrize("resource", sorted(COUNTS))
async def test_list_is_complete_and_ordered(api: ApiClient, resource: str) -> None:
    response = await api.get(f"{API}/{resource}")
    assert response.status_code == 200
    assert response.headers["content-type"] == SUCCESS_TYPE
    assert response.headers["api-supported-versions"] == "1.0"
    items = response.json()
    assert [item["id"] for item in items] == list(range(1, COUNTS[resource] + 1))


async def test_books_follow_their_rules(api: ApiClient) -> None:
    before = datetime.now(UTC)
    books = (await api.get(f"{API}/Books")).json()
    for book in books:
        number = book["id"]
        assert book["title"] == f"Book {number}"
        assert book["pageCount"] == number * 100
        assert book["description"] == LOREM
        assert book["excerpt"] == LOREM * 5
        published = parse_time(book["publishDate"])
        assert abs(published - (before - timedelta(days=number))) < timedelta(minutes=2)
        assert book["publishDate"].endswith("+00:00")


async def test_activities_follow_their_rules(api: ApiClient) -> None:
    before = datetime.now(UTC)
    activities = (await api.get(f"{API}/Activities")).json()
    for activity in activities:
        number = activity["id"]
        assert activity["title"] == f"Activity {number}"
        assert activity["completed"] is (number % 2 == 0)
        due = parse_time(activity["dueDate"])
        assert abs(due - (before + timedelta(hours=number))) < timedelta(minutes=2)


async def test_users_and_cover_photos_follow_their_rules(api: ApiClient) -> None:
    users = (await api.get(f"{API}/Users")).json()
    assert users == [
        {"id": n, "userName": f"User {n}", "password": f"Password{n}"} for n in range(1, 11)
    ]
    covers = (await api.get(f"{API}/CoverPhotos")).json()
    for cover in covers:
        assert cover["idBook"] == cover["id"]
        assert cover["url"] == (
            f"https://placeholdit.imgix.net/~text?txtsize=33&txt=Book {cover['id']}&w=250&h=350"
        )


async def test_query_parameters_are_ignored(api: ApiClient) -> None:
    plain = (await api.get(f"{API}/Users")).json()
    filtered = await api.get(f"{API}/Users", params={"id": "1", "limit": "2", "sort": "-id"})
    assert filtered.status_code == 200
    assert filtered.json() == plain


async def test_timestamps_move_with_the_clock(api: ApiClient) -> None:
    first = (await api.get(f"{API}/Activities/1")).json()["dueDate"]
    second = (await api.get(f"{API}/Activities/1", params={"again": "1"})).json()["dueDate"]
    assert parse_time(second) >= parse_time(first)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,7})?\+00:00", second)


# -- items ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource", "key"),
    [
        ("Activities", 1),
        ("Activities", 2),
        ("Activities", 30),
        ("Books", 1),
        ("Books", 57),
        ("Books", 200),
        ("CoverPhotos", 1),
        ("CoverPhotos", 133),
        ("CoverPhotos", 200),
        ("Users", 1),
        ("Users", 10),
    ],
)
async def test_item_matches_list(api: ApiClient, resource: str, key: int) -> None:
    listing = (await api.get(f"{API}/{resource}")).json()
    response = await api.get(f"{API}/{resource}/{key}")
    assert response.status_code == 200
    assert response.headers["content-type"] == SUCCESS_TYPE
    item = response.json()
    expected = next(entry for entry in listing if entry["id"] == key)
    for field, value in expected.items():
        if field in {"publishDate", "dueDate"}:
            assert abs(parse_time(item[field]) - parse_time(value)) < timedelta(minutes=1)
        else:
            assert item[field] == value


@pytest.mark.parametrize(
    ("resource", "key"),
    [
        ("Activities", "31"),
        ("Activities", "0"),
        ("Books", "201"),
        ("Books", "-1"),
        ("Books", "2147483647"),
        ("Books", "-2147483648"),
        ("CoverPhotos", "201"),
        ("Users", "11"),
        ("Users", "-0"),
        ("Authors", "100000"),
        ("Authors", "0"),
    ],
)
async def test_unknown_item_is_404(api: ApiClient, resource: str, key: str) -> None:
    assert_not_found(await api.get(f"{API}/{resource}/{key}"))


@pytest.mark.parametrize(
    ("resource", "raw", "sent"),
    [
        ("Books", "abc", "abc"),
        ("Books", "1.5", "1.5"),
        ("Books", "1e2", "1e2"),
        ("Books", "2147483648", "2147483648"),
        ("Activities", "-2147483649", "-2147483649"),
        ("Users", "true", "true"),
        ("CoverPhotos", "0xZZ", "0xZZ"),
        ("CoverPhotos", "0x123456789", "0x123456789"),
        ("Authors", "one", "one"),
        ("Users", "€", "%E2%82%AC"),
    ],
)
async def test_malformed_id_is_400(api: ApiClient, resource: str, raw: str, sent: str) -> None:
    assert_invalid(await api.get(f"{API}/{resource}/{sent}"), "id", raw)


@pytest.mark.parametrize("sent", ["0x", "%23", "%26h", "0x-1"])
async def test_empty_or_signed_hex_id_is_400(api: ApiClient, sent: str) -> None:
    assert_invalid(
        await api.get(f"{API}/Users/{sent}"), "id", sent, message="The input was not valid."
    )


@pytest.mark.parametrize(
    ("sent", "expected"),
    [("+5", 5), ("01", 1), ("%201", 1), ("7%20", 7), ("0x10", 16), ("0XA", 10), ("-0", None)],
)
async def test_lenient_id_binding(api: ApiClient, sent: str, expected: int | None) -> None:
    response = await api.get(f"{API}/Books/{sent}")
    if expected is None:
        assert_not_found(response)
        return
    assert response.status_code == 200
    assert response.json()["id"] == expected


# -- authors (random on every request) -------------------------------------------------------


def assert_authors(authors: list[dict[str, Any]]) -> None:
    assert [a["id"] for a in authors] == list(range(1, len(authors) + 1))
    per_book: dict[int, int] = {}
    for author in authors:
        assert author["firstName"] == f"First Name {author['id']}"
        assert author["lastName"] == f"Last Name {author['id']}"
        per_book[author["idBook"]] = per_book.get(author["idBook"], 0) + 1
    assert list(per_book) == list(range(1, 201))
    assert set(per_book.values()) <= {2, 3, 4}


async def test_authors_cover_every_book(api: ApiClient) -> None:
    response = await api.get(f"{API}/Authors", compare=RANDOM_AUTHORS)
    assert response.status_code == 200
    assert response.headers["content-type"] == SUCCESS_TYPE
    authors = response.json()
    assert 400 <= len(authors) <= 800
    assert_authors(authors)


@pytest.mark.parametrize("key", [1, 2])
async def test_first_authors_always_belong_to_book_1(api: ApiClient, key: int) -> None:
    response = await api.get(f"{API}/Authors/{key}")
    assert response.status_code == 200
    assert response.json() == {
        "id": key,
        "idBook": 1,
        "firstName": f"First Name {key}",
        "lastName": f"Last Name {key}",
    }


async def test_author_in_the_middle(api: ApiClient) -> None:
    response = await api.get(f"{API}/Authors/300", compare=RANDOM_AUTHORS)
    assert response.status_code == 200
    author = response.json()
    assert author["id"] == 300
    assert author["firstName"] == "First Name 300"
    assert 75 <= author["idBook"] <= 150


@pytest.mark.parametrize("book", [1, 2, 3, 100, 200])
async def test_authors_of_a_book(api: ApiClient, book: int) -> None:
    response = await api.get(f"{API}/Authors/authors/books/{book}", compare=RANDOM_AUTHORS)
    assert response.status_code == 200
    authors = response.json()
    assert 2 <= len(authors) <= 4
    ids = [a["id"] for a in authors]
    assert ids == list(range(ids[0], ids[0] + len(ids)))
    assert all(a["idBook"] == book for a in authors)
    assert 2 * (book - 1) + 1 <= ids[0] <= 4 * (book - 1) + 1


@pytest.mark.parametrize("book", ["0", "-1", "201", "2147483647"])
async def test_authors_of_unknown_book_is_empty(api: ApiClient, book: str) -> None:
    response = await api.get(f"{API}/Authors/authors/books/{book}")
    assert response.status_code == 200
    assert response.json() == []


async def test_authors_of_malformed_book_is_400(api: ApiClient) -> None:
    assert_invalid(await api.get(f"{API}/Authors/authors/books/abc"), "idBook", "abc")
    assert_invalid(await api.get(f"{API}/Authors/authors/books/2147483648"), "idBook", "2147483648")


# -- cover photos by book ------------------------------------------------------------------------


@pytest.mark.parametrize("book", ["1", "42", "200", "0x10", "+9"])
async def test_covers_of_a_book(api: ApiClient, book: str) -> None:
    response = await api.get(f"{API}/CoverPhotos/books/covers/{book}")
    assert response.status_code == 200
    covers = response.json()
    assert len(covers) == 1
    number = int(book, 16) if book.startswith("0x") else int(book)
    assert covers[0]["idBook"] == number


@pytest.mark.parametrize("book", ["0", "-1", "201"])
async def test_covers_of_unknown_book_is_empty(api: ApiClient, book: str) -> None:
    response = await api.get(f"{API}/CoverPhotos/books/covers/{book}")
    assert response.status_code == 200
    assert response.json() == []


async def test_covers_of_malformed_book_is_400(api: ApiClient) -> None:
    assert_invalid(await api.get(f"{API}/CoverPhotos/books/covers/1.0"), "idBook", "1.0")


async def test_every_cover_points_to_a_book(api: ApiClient) -> None:
    books = {book["id"] for book in (await api.get(f"{API}/Books")).json()}
    covers = (await api.get(f"{API}/CoverPhotos")).json()
    assert {cover["idBook"] for cover in covers} <= books
    for cover in covers[:3]:
        book = await api.get(f"{API}/Books/{cover['idBook']}")
        assert book.status_code == 200
        assert book.json()["title"] in cover["url"]


async def test_book_with_its_authors_and_cover(api: ApiClient) -> None:
    book = (await api.get(f"{API}/Books/12")).json()
    authors = (
        await api.get(f"{API}/Authors/authors/books/{book['id']}", compare=RANDOM_AUTHORS)
    ).json()
    covers = (await api.get(f"{API}/CoverPhotos/books/covers/{book['id']}")).json()
    assert authors
    assert all(author["idBook"] == book["id"] for author in authors)
    assert [cover["idBook"] for cover in covers] == [book["id"]]


# -- routing -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/books",
        "/API/V1/BOOKS/3",
        "/api/v1/Books/",
        "/api/v1/users/2/",
        "/api/v1/authors/AUTHORS/Books/5",
        "/api/v1/coverphotos/BOOKS/covers/9",
    ],
)
async def test_routes_ignore_case_and_trailing_slash(api: ApiClient, path: str) -> None:
    options = RANDOM_AUTHORS if "authors" in path else None
    response = await api.get(path, compare=options)
    assert response.status_code == 200
    assert response.headers["content-type"] == SUCCESS_TYPE


@pytest.mark.parametrize("path", ["/api/v1/Nope", "/api/v2/Books", "/api/v1/Books/1/extra"])
async def test_unknown_route_is_empty_404(api: ApiClient, path: str) -> None:
    response = await api.get(path)
    assert response.status_code == 404
    assert response.content == b""
    assert "content-type" not in response.headers


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("HEAD", "/api/v1/Books/1", "DELETE, GET, PUT"),
        ("OPTIONS", "/api/v1/Users/1", "DELETE, GET, PUT"),
        ("OPTIONS", "/api/v1/Activities", "GET, POST"),
        ("HEAD", "/api/v1/Authors/authors/books/1", "GET"),
    ],
)
async def test_other_methods_are_405(api: ApiClient, method: str, path: str, allow: str) -> None:
    response = await api.request(method, path)
    assert response.status_code == 405
    assert response.headers["allow"] == allow
    assert response.content == b""


async def test_api_version_in_query_or_header_is_ignored(api: ApiClient) -> None:
    by_query = await api.get(f"{API}/Users/1", params={"api-version": "2.0"})
    by_header = await api.get(f"{API}/Users/1", headers={"api-version": "2.0"})
    assert by_query.status_code == by_header.status_code == 200
    assert by_query.json() == by_header.json()


async def test_accept_header_does_not_change_the_answer(api: ApiClient) -> None:
    for accept in ("text/plain", "application/xml", "*/*"):
        response = await api.get(f"{API}/Users/3", headers={"Accept": accept})
        assert response.status_code == 200
        assert response.headers["content-type"] == SUCCESS_TYPE


# -- ASP.NET pipeline quirks (recorded in review-probes) ---------------------------------------


async def test_accept_text_json_gets_text_json(api: ApiClient) -> None:
    response = await api.get(f"{API}/Users/1", headers={"Accept": "text/json"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/json; charset=utf-8; v=1.0"


@pytest.mark.parametrize("header", ["Accept", "Content-Type"])
async def test_unsupported_api_version_on_reads(api: ApiClient, header: str) -> None:
    # The message names the request URI, so the host differs between the two sides by design.
    options = CompareOptions(policies=[IgnorePolicy(path="error.message", scope="body")])
    response = await api.get(
        f"{API}/Books/2", headers={header: "application/json; v=3.0"}, compare=options
    )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    error = response.json()["error"]
    assert error["code"] == "UnsupportedApiVersion"
    assert error["message"].endswith("/api/v1/Books/2' does not support the API version '3.0'.")


@pytest.mark.parametrize("version", ["1.0", "1", "abc"])
async def test_supported_or_unreadable_versions_pass(api: ApiClient, version: str) -> None:
    response = await api.get(f"{API}/Users/4", headers={"Accept": f"application/json; v={version}"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("sent", "expected"),
    [("%C2%A03", 3), ("0x+1", 1), ("0x000000010", 16), ("&H1f", 31)],
)
async def test_more_lenient_id_binding(api: ApiClient, sent: str, expected: int) -> None:
    response = await api.get(f"{API}/Books/{sent}")
    assert response.status_code == 200
    assert response.json()["id"] == expected


async def test_empty_segments_collapse(api: ApiClient) -> None:
    # Parity can't map "//8" to the endpoint (and its time policies): compare the shape here,
    # the id below.
    response = await api.get(f"{API}/Books//8", compare=CompareOptions(values=False))
    assert response.status_code == 200
    assert response.json()["id"] == 8
    assert (await api.get(f"{API}/Books/1%2F2")).status_code == 404


async def test_root_redirects_to_swagger_ui(api: ApiClient) -> None:
    root = await api.get("/", follow_redirects=False)
    assert root.status_code == 301
    assert root.headers["location"] == "index.html"
    page = await api.get("/index.html", compare=CompareOptions(values=False))
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<title>Swagger UI</title>" in page.text
