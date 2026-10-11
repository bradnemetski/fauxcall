"""End-to-end write scenarios: bodies are bound and echoed, nothing is ever stored.

The real API was approved for at most 20 calls per write endpoint (recording included), so these
scenarios only use endpoints with budget left and send each write once per run; run them against
the real API (``--allow-real-writes``) sparingly. ``POST /Books``, ``PUT /Books/{id}`` and
``PUT /Activities/{id}`` used their whole budget while recording; the unit test
``tests/unit/test_recorded_writes.py`` replays those recordings byte for byte instead.

Real-side calls per run: POST Activities 3, POST Authors 3, POST CoverPhotos 4, POST Users 5,
PUT Authors 2, PUT CoverPhotos 2, PUT Users 2, DELETE Books 2, DELETE others 1 each.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from fauxcall.model.fields import IgnorePolicy
from fauxcall.testing import CompareOptions

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient

API = "/api/v1"
JSON = {"content-type": "application/json"}
SUCCESS_TYPE = "application/json; charset=utf-8; v=1.0"
PROBLEM_TYPE = "application/problem+json; charset=utf-8"
TRACE_ID = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-00")
RANDOM_AUTHORS = CompareOptions(values=False, lengths=False)


def assert_echo(response: Any, expected: dict[str, Any]) -> None:
    assert response.status_code == 200
    assert response.headers["content-type"] == SUCCESS_TYPE
    assert response.headers["api-supported-versions"] == "1.0"
    assert response.json() == expected
    assert list(response.json()) == list(expected)


def assert_errors(response: Any, errors: dict[str, list[str]]) -> None:
    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM_TYPE
    body = response.json()
    assert body["type"] == "https://tools.ietf.org/html/rfc7231#section-6.5.1"
    assert body["title"] == "One or more validation errors occurred."
    assert body["status"] == 400
    assert TRACE_ID.fullmatch(body["traceId"])
    assert body["errors"] == errors


async def test_created_user_is_echoed_but_not_stored(api: ApiClient) -> None:
    sent = b'{"password":"fauxcall-probe","USERNAME":"fauxcall-probe","id":5001,"extra":[1,{}]}'
    created = await api.post(f"{API}/Users", content=sent, headers=JSON)
    assert_echo(created, {"id": 5001, "userName": "fauxcall-probe", "password": "fauxcall-probe"})
    readback = await api.get(f"{API}/Users/5001")
    assert readback.status_code == 404
    listing = (await api.get(f"{API}/Users")).json()
    assert len(listing) == 10


async def test_user_body_errors(api: ApiClient) -> None:
    trailing = await api.post(f"{API}/Users", content=b'{"id":1,}', headers=JSON)
    assert_errors(
        trailing,
        {
            "$": [
                (
                    "The JSON object contains a trailing comma at the end which is not supported in"
                    " this mode. Change the reader options. Path: $ | LineNumber: 0"
                    " | BytePositionInLine: 8."
                )
            ]
        },
    )
    wrong = await api.post(f"{API}/Users", content=b'{"id":1,"userName":["a"]}', headers=JSON)
    assert_errors(
        wrong,
        {
            "$.userName": [
                (
                    "The JSON value could not be converted to System.String. Path: $.userName"
                    " | LineNumber: 0 | BytePositionInLine: 20."
                )
            ]
        },
    )


async def test_non_json_bodies_are_415(api: ApiClient) -> None:
    response = await api.post(
        f"{API}/Users", content=b'{"id":1}', headers={"content-type": "text/plain"}
    )
    assert response.status_code == 415
    assert response.headers["content-type"] == PROBLEM_TYPE
    body = response.json()
    assert body["title"] == "Unsupported Media Type"
    assert body["type"] == "https://tools.ietf.org/html/rfc7231#section-6.5.13"


async def test_unsupported_api_version_in_media_type(api: ApiClient) -> None:
    # The message names the request URI, so the host differs between the two sides by design.
    options = CompareOptions(policies=[IgnorePolicy(path="error.message", scope="body")])
    response = await api.post(
        f"{API}/Users",
        content=b'{"id":1}',
        headers={"content-type": "application/json; v=2.0"},
        compare=options,
    )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    error = response.json()["error"]
    assert error["code"] == "UnsupportedApiVersion"
    assert error["message"].endswith("/api/v1/Users' does not support the API version '2.0'.")
    assert error["innerError"] is None


async def test_author_binding_rules(api: ApiClient) -> None:
    lenient = await api.post(
        f"{API}/Authors", content=b'{"Id":"12","idbook":"+3","firstName":null}', headers=JSON
    )
    assert_echo(lenient, {"id": 12, "idBook": 3, "firstName": None, "lastName": None})
    empty = await api.post(f"{API}/Authors", content=b"{}", headers=JSON)
    assert_echo(empty, {"id": 0, "idBook": 0, "firstName": None, "lastName": None})
    overflow = await api.post(f"{API}/Authors", content=b'{"idBook":2147483648}', headers=JSON)
    assert_errors(
        overflow,
        {
            "$.idBook": [
                (
                    "The JSON value could not be converted to System.Int32. Path: $.idBook"
                    " | LineNumber: 0 | BytePositionInLine: 20."
                )
            ]
        },
    )


async def test_cover_photo_bodies(api: ApiClient) -> None:
    echoed = await api.post(
        f"{API}/CoverPhotos",
        content='{"id":7,"idBook":7,"url":"https://example.com/é <x>&y"}'.encode(),
        headers=JSON,
    )
    assert_echo(echoed, {"id": 7, "idBook": 7, "url": "https://example.com/é <x>&y"})
    number = await api.post(f"{API}/CoverPhotos", content=b'{"url":5}', headers=JSON)
    assert_errors(
        number,
        {
            "$.url": [
                (
                    "The JSON value could not be converted to System.Uri. Path: $.url"
                    " | LineNumber: 0 | BytePositionInLine: 8."
                )
            ]
        },
    )
    missing = await api.post(f"{API}/CoverPhotos", content=b"", headers=JSON)
    assert_errors(missing, {"": ["A non-empty request body is required."]})
    garbage = await api.post(f"{API}/CoverPhotos", content=b"\n\n  oops", headers=JSON)
    assert_errors(
        garbage,
        {
            "$": [
                (
                    "'o' is an invalid start of a value. Path: $ | LineNumber: 2"
                    " | BytePositionInLine: 2."
                )
            ]
        },
    )


async def test_activity_dates(api: ApiClient) -> None:
    offset = await api.post(
        f"{API}/Activities",
        content=b'{"id":3,"dueDate":"2026-03-01T00:30:00.25+01:00","completed":true}',
        headers=JSON,
    )
    assert_echo(
        offset,
        {"id": 3, "title": None, "dueDate": "2026-02-28T23:30:00.25+00:00", "completed": True},
    )
    utc = await api.post(
        f"{API}/Activities", content=b'{"dueDate":"2026-12-31T23:59:59.99999999Z"}', headers=JSON
    )
    assert utc.json()["dueDate"] == "2026-12-31T23:59:59.9999999Z"
    bad = await api.post(f"{API}/Activities", content=b'{"dueDate":"31/12/2026"}', headers=JSON)
    assert_errors(
        bad,
        {
            "$.dueDate": [
                (
                    "The JSON value could not be converted to System.DateTime. Path: $.dueDate"
                    " | LineNumber: 0 | BytePositionInLine: 23."
                )
            ]
        },
    )


async def test_replacing_echoes_the_body_id(api: ApiClient) -> None:
    replaced = await api.put(
        f"{API}/Authors/3", content=b'{"id":9,"idBook":1,"lastName":"fauxcall-probe"}', headers=JSON
    )
    assert_echo(replaced, {"id": 9, "idBook": 1, "firstName": None, "lastName": "fauxcall-probe"})
    readback = await api.get(f"{API}/Authors/3", compare=RANDOM_AUTHORS)
    assert readback.json()["lastName"] == "Last Name 3"
    both = await api.put(f"{API}/Authors/x1", content=b"[1]", headers=JSON)
    assert_errors(
        both,
        {
            "$": [
                (
                    "The JSON value could not be converted to FakeRestAPI.Models.Author. Path: $"
                    " | LineNumber: 0 | BytePositionInLine: 1."
                )
            ],
            "id": ["The value 'x1' is not valid."],
        },
    )


async def test_replaced_user_is_not_stored(api: ApiClient) -> None:
    replaced = await api.put(
        f"{API}/Users/2", content=b'{"id":2,"userName":"fauxcall-probe"}', headers=JSON
    )
    assert_echo(replaced, {"id": 2, "userName": "fauxcall-probe", "password": None})
    readback = await api.get(f"{API}/Users/2")
    assert readback.json() == {"id": 2, "userName": "User 2", "password": "Password2"}
    unknown = await api.put(f"{API}/Users/999", content=b'{"id":999}', headers=JSON)
    assert_echo(unknown, {"id": 999, "userName": None, "password": None})


async def test_replace_cover_photo_errors(api: ApiClient) -> None:
    multiline = await api.put(
        f"{API}/CoverPhotos/5", content=b'{\r\n "id": 5,\r\n "idBook": tru\r\n}', headers=JSON
    )
    assert_errors(
        multiline,
        {
            "$.idBook": [
                (
                    "'tru\r\n}' is an invalid JSON literal. Expected the literal 'true'."
                    " Path: $.idBook | LineNumber: 2 | BytePositionInLine: 14."
                )
            ]
        },
    )
    leftover = await api.put(f"{API}/CoverPhotos/5", content=b'{"id":5} {}', headers=JSON)
    assert_errors(
        leftover,
        {
            "$": [
                (
                    "'{' is invalid after a single JSON value. Expected end of data. Path: $"
                    " | LineNumber: 0 | BytePositionInLine: 9."
                )
            ]
        },
    )


async def test_deleting_changes_nothing(api: ApiClient) -> None:
    for path in (
        f"{API}/Books/1",
        f"{API}/Books/99999",
        f"{API}/Activities/0x2",
        f"{API}/Authors/1",
        f"{API}/CoverPhotos/-7",
    ):
        response = await api.delete(path)
        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["api-supported-versions"] == "1.0"
        assert "content-type" not in response.headers
    assert (await api.get(f"{API}/Books/1")).status_code == 200
    invalid = await api.delete(f"{API}/Users/abc")
    assert_errors(invalid, {"id": ["The value 'abc' is not valid."]})
