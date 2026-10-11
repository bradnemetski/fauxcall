"""The System.Text.Json and ASP.NET binding rules, case by case (offline)."""

# ruff: noqa: E501 - the refused-body table reads best one case per line

from __future__ import annotations

import random

import pytest

from fauxcall_fakerestapi import aspnet, stj
from fauxcall_fakerestapi.app import AspNetQuirks
from fauxcall_fakerestapi.handlers import DTOS, generate_authors

BOOK = DTOS["Books"]
ACTIVITY = DTOS["Activities"]
COVER = DTOS["CoverPhotos"]


def failure(body: bytes, dto: stj.Dto = BOOK) -> tuple[str, str]:
    with pytest.raises(stj.BindingError) as caught:
        stj.bind(body, dto)
    return caught.value.key, caught.value.message


def where(line: int, column: int, path: str = "$") -> str:
    return f" Path: {path} | LineNumber: {line} | BytePositionInLine: {column}."


# -- successful binding ----------------------------------------------------------------------


def test_defaults_case_insensitive_names_and_duplicates() -> None:
    values = stj.bind(b'{"ID":1,"id":2,"TITLE":"t","bogus":{"a":[1,{"b":null},[]],"c":{}}}', BOOK)
    assert values == {
        "id": 2,
        "title": "t",
        "description": None,
        "pageCount": 0,
        "excerpt": None,
        "publishDate": "0001-01-01T00:00:00",
    }


def test_numbers_from_strings_and_signs() -> None:
    assert stj.bind(b'{"id":"+7","pageCount":"-0012"}', BOOK)["pageCount"] == -12
    assert stj.bind(b'{"id":-0}', BOOK)["id"] == 0
    assert stj.bind(b'\xef\xbb\xbf  {"id" : 3 }  ', BOOK)["id"] == 3


@pytest.mark.parametrize(
    ("given", "echoed"),
    [
        ("2026-01-02", "2026-01-02T00:00:00"),
        ("2026-01-02T03:04", "2026-01-02T03:04:00"),
        ("2026-01-02T03:04:05.5", "2026-01-02T03:04:05.5"),
        ("2026-01-02T03:04:05.1000000Z", "2026-01-02T03:04:05.1Z"),
        ("2026-01-02T03:04:05.123456789Z", "2026-01-02T03:04:05.1234567Z"),
        ("2026-01-02T03:04:05.1234567+02:00", "2026-01-02T01:04:05.1234567+00:00"),
        ("2026-01-02T03:04:05-05:30", "2026-01-02T08:34:05+00:00"),
        ("0099-01-01T00:00:00", "0099-01-01T00:00:00"),
    ],
)
def test_datetimes(given: str, echoed: str) -> None:
    assert stj.parse_datetime(given) == echoed


@pytest.mark.parametrize(
    "given",
    ["2026-1-2", "2026-01-02 03:04:05", "2026-13-01", "0000-01-01", "0001-01-01T00:00:00+01:00"],
)
def test_bad_datetimes(given: str) -> None:
    assert stj.parse_datetime(given) is None


def test_booleans_and_uris() -> None:
    assert stj.bind(b'{"completed":true}', ACTIVITY)["completed"] is True
    assert stj.bind(b'{"completed":false}', ACTIVITY)["completed"] is False
    assert stj.bind(b'{"url":"not a url"}', COVER)["url"] == "not a url"
    assert stj.bind(b'{"url":null}', COVER)["url"] is None


def test_serialize_relaxed_escaping() -> None:
    values = stj.bind('{"title":"é€<>&\'+`\\"\\\\/\\u0001\\b\\f\\r😀"}'.encode(), BOOK)
    text = stj.serialize(values, BOOK)
    assert '"title":"é€<>&\'+`\\"\\\\/\\u0001\\b\\f\\r\\uD83D\\uDE00"' in text
    assert stj.serialize({**values, "title": "\ud800"}, BOOK).count("\\uD800") == 1


# -- refused bodies -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "key", "message"),
    [
        (b"", "", stj.EMPTY_BODY),
        (b"null", "", stj.EMPTY_BODY),
        (
            b"[]",
            "$",
            "The JSON value could not be converted to FakeRestAPI.Models.Book." + where(0, 1),
        ),
        (
            b"true",
            "$",
            "The JSON value could not be converted to FakeRestAPI.Models.Book." + where(0, 4),
        ),
        (
            b"null x",
            "$",
            "'x' is invalid after a single JSON value. Expected end of data." + where(0, 5),
        ),
        (
            b"{}}",
            "$",
            "'}' is invalid after a single JSON value. Expected end of data." + where(0, 2),
        ),
        (b"+1", "$", "'+' is an invalid start of a value." + where(0, 0)),
        (b"\xc3\xa9", "$", "'é' is an invalid start of a value." + where(0, 0)),
        (b"{  ", "$", stj._DEPTH + where(0, 3)),
        (b'{"id":1 ', "$.id", stj._DEPTH + where(0, 8, "$.id")),
        (b'{"id":', "$", "Expected a value, but instead reached end of data." + where(0, 6)),
        (b'{"ti', "$", "Expected end of string, but instead reached end of data." + where(0, 4)),
        (
            b'{"title":"a\\',
            "$.title",
            "Expected end of string, but instead reached end of data." + where(0, 12, "$.title"),
        ),
        (
            b'{"title":"a\\u12g4"}',
            "$.title",
            "'g' is not a hex digit following '\\u' within a JSON string. The string should be correctly escaped."
            + where(0, 15, "$.title"),
        ),
        (
            b'{"title":"a\nb"}',
            "$.title",
            "'0x0A' is invalid within a JSON string. The string should be correctly escaped."
            + where(0, 11, "$.title"),
        ),
        (
            b'{"id":-}',
            "$.id",
            "'}' is invalid within a number, immediately after a sign character ('+' or '-'). Expected a digit ('0'-'9')."
            + where(0, 7, "$.id"),
        ),
        (
            b'{"id":-',
            "$.id",
            "Expected a digit ('0'-'9'), but instead reached end of data." + where(0, 7, "$.id"),
        ),
        (b'{"id":01}', "$.id", "Invalid leading zero before '1'." + where(0, 7, "$.id")),
        (
            b'{"id":1.}',
            "$.id",
            "'}' is invalid within a number, immediately after a decimal point ('.'). Expected a digit ('0'-'9')."
            + where(0, 8, "$.id"),
        ),
        (
            b'{"id":1e',
            "$.id",
            "Expected a digit ('0'-'9'), but instead reached end of data." + where(0, 8, "$.id"),
        ),
        (
            b'{"id":1e+5}',
            "$.id",
            "The JSON value could not be converted to System.Int32." + where(0, 10, "$.id"),
        ),
        (
            b'{"id":2147483648}',
            "$.id",
            "The JSON value could not be converted to System.Int32." + where(0, 16, "$.id"),
        ),
        (
            b'{"id":"1.5"}',
            "$.id",
            "The JSON value could not be converted to System.Int32." + where(0, 11, "$.id"),
        ),
        (
            b'{"id":nul }',
            "$.id",
            "'nul }' is an invalid JSON literal. Expected the literal 'null'."
            + where(0, 9, "$.id"),
        ),
        (
            b'{"id":fals',
            "$.id",
            "'fals' is an invalid JSON literal. Expected the literal 'false'."
            + where(0, 10, "$.id"),
        ),
        (
            b'{"id":[1]}',
            "$.id",
            "The JSON value could not be converted to System.Int32." + where(0, 7, "$.id"),
        ),
        (
            b'{"title":true}',
            "$.title",
            "The JSON value could not be converted to System.String." + where(0, 13, "$.title"),
        ),
        (
            b'{"publishDate":5}',
            "$.publishDate",
            "The JSON value could not be converted to System.DateTime."
            + where(0, 16, "$.publishDate"),
        ),
        (
            b'{"x":{"a":1,}}',
            "$.x",
            "The JSON object contains a trailing comma at the end which is not supported in this mode. Change the reader options."
            + where(0, 12, "$.x"),
        ),
        (
            b'{"x":[1 2]}',
            "$.x",
            "'2' is invalid after a value. Expected either ',', '}', or ']'." + where(0, 8, "$.x"),
        ),
        (
            b'{"x":[1,',
            "$.x",
            "Expected start of a property name or value, but instead reached end of data."
            + where(0, 7, "$.x"),
        ),
        (b'{"x":[', "$.x", stj._DEPTH + where(0, 6, "$.x")),
        (
            b'{"x":{"a":1',
            "$.x",
            "'1' is an invalid end of a number. Expected a delimiter." + where(0, 11, "$.x"),
        ),
        (
            b'{"x":{"a" 1}}',
            "$.x",
            "'1' is invalid after a property name. Expected a ':'." + where(0, 10, "$.x"),
        ),
        (b'{"x":{}', "$.x", stj._DEPTH + where(0, 7, "$.x")),
        (
            b'{\n  "x": 1,\n  y}',
            "$",
            "'y' is an invalid start of a property name. Expected a '\"'." + where(2, 2),
        ),
    ],
)
def test_refused(body: bytes, key: str, message: str) -> None:
    assert failure(body) == (key, message)


def test_activity_bool_conversion() -> None:
    assert failure(b'{"completed":"true"}', ACTIVITY) == (
        "$.completed",
        "The JSON value could not be converted to System.Boolean." + where(0, 19, "$.completed"),
    )


# -- route values and media types ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "bound"),
    [
        ("1", 1),
        ("+5", 5),
        ("01", 1),
        (" 1", 1),
        ("1 ", 1),
        ("-0", 0),
        ("0x10", 16),
        ("0XfF", 255),
        ("#10", 16),
        ("&h7FFFFFFF", 2147483647),
        ("0xFFFFFFFF", -1),
        ("2147483647", 2147483647),
        ("-2147483648", -2147483648),
        ("2147483648", None),
        ("-2147483649", None),
        ("1e2", None),
        ("1.5", None),
        ("abc", None),
        ("0x", None),
        ("0x123456789", None),
        ("0x000000010", 16),
        ("0x+1", 1),
        ("\u00a01\u2003", 1),
        ("", None),
        (None, None),
    ],
)
def test_bind_int32(raw: str | None, bound: int | None) -> None:
    assert aspnet.bind_int32(raw) == bound


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("abc", "The value 'abc' is not valid."),
        ("0xZZ", "The value '0xZZ' is not valid."),
        ("0x", "The input was not valid."),
        (" # ", "The input was not valid."),
        ("&h-1", "The input was not valid."),
    ],
)
def test_invalid_value_messages(raw: str, message: str) -> None:
    assert aspnet.invalid_value(raw) == message


def test_versions_and_negotiation() -> None:
    assert aspnet.requested_version("application/json; v=2.0", None) == "2.0"
    assert aspnet.requested_version(None, "text/plain, application/json; v=3") == "3"
    assert aspnet.requested_version("application/json; v=1.0", "application/json; v=abc") is None
    assert aspnet.prefers_text_json("text/json, application/json")
    assert not aspnet.prefers_text_json("application/json, text/json")
    assert not aspnet.prefers_text_json(None)
    assert aspnet.unreadable_charset({"charset": "UTF-16LE"})
    assert not aspnet.unreadable_charset({"charset": "utf-8"})


def test_depth_limit_and_escaping() -> None:
    deep = b'{"x":' + b'{"a":' * 32 + b"1" + b"}" * 32 + b"}"
    assert failure(deep)[1].startswith(
        "The maximum configured depth of 32 has been exceeded. Cannot read next JSON object."
    )
    assert stj.bind(b'{"x":' + b"[" * 30 + b"]" * 30 + b"}", BOOK)["id"] == 0
    assert failure(b'{"title":"\\udc00"}')[0] == "$.title"
    values = {**stj.bind(b"{}", BOOK), "title": "\x7f\u2028\ue000\u00ad"}
    assert '"title":"\\u007F\\u2028\\uE000\u00ad"' in stj.serialize(values, BOOK)


def test_media_types() -> None:
    assert aspnet.media_type(None) == ("", {})
    assert aspnet.media_type('Application/JSON; charset=utf-8; V="2.0"') == (
        "application/json",
        {"charset": "utf-8", "v": "2.0"},
    )
    assert aspnet.accepts_json("text/json")
    assert aspnet.accepts_json("application/merge-patch+json")
    assert not aspnet.accepts_json("text/plain")
    assert not aspnet.accepts_json("")
    assert aspnet.unsupported_version({"v": "2.0"}) == "2.0"
    assert aspnet.unsupported_version({"v": "1.0"}) is None
    assert aspnet.unsupported_version({}) is None


# -- authors ---------------------------------------------------------------------------------


def test_generated_authors_are_numbered_per_book() -> None:
    authors = generate_authors(list(range(1, 201)), random.Random(1))
    assert 400 <= len(authors) <= 800
    ids = [a["id"] for a in authors if isinstance(a, dict)]
    assert ids == list(range(1, len(authors) + 1))
    per_book: dict[int, int] = {}
    for author in authors:
        assert isinstance(author, dict)
        assert author["firstName"] == f"First Name {author['id']}"
        assert author["lastName"] == f"Last Name {author['id']}"
        book = author["idBook"]
        assert isinstance(book, int)
        per_book[book] = per_book.get(book, 0) + 1
    assert set(per_book) == set(range(1, 201))
    assert set(per_book.values()) <= {2, 3, 4}


# -- the Allow header on 405s ---------------------------------------------------------------


async def test_allow_header_passes_other_scopes_through() -> None:
    seen: list[str] = []

    async def inner(scope: object, receive: object, send: object) -> None:
        del receive, send
        assert isinstance(scope, dict)
        seen.append(scope["type"])

    middleware = AspNetQuirks(inner)
    await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


async def test_allow_header_skips_unknown_paths() -> None:
    sent: list[dict[str, object]] = []

    async def inner(scope: object, receive: object, send: object) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 405, "headers": []})  # type: ignore[operator]

    async def collect(message: dict[str, object]) -> None:
        sent.append(message)

    middleware = AspNetQuirks(inner)
    await middleware({"type": "http", "path": "/nowhere"}, None, collect)  # type: ignore[arg-type]
    assert sent == [{"type": "http.response.start", "status": 405, "headers": []}]
