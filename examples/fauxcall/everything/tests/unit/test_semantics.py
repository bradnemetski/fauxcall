"""The JavaScript and zod semantics the handlers rely on, checked against what Node and zod do."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from fauxcall_everything import js, validation, zod


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (0.1 + 0.2, "0.30000000000000004"),
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (1e-7, "1e-7"),
        (1e-6, "0.000001"),
        (-0.0, "0"),
        (-2.5, "-2.5"),
        (1.7976931348623157e308 * 2, "Infinity"),
        (-math.inf, "-Infinity"),
        (math.nan, "NaN"),
        (123e-20, "1.23e-18"),
        (5.0, "5"),
    ],
)
def test_js_number(number: float, text: str) -> None:
    assert js.js_number(number) == text


def test_as_number_follows_doubles() -> None:
    assert js.as_number(2**53 + 1) == 2**53
    assert js.as_number(10**400) == math.inf
    assert js.as_number(-(10**400)) == -math.inf
    with pytest.raises(TypeError):
        js.as_number(True)  # noqa: FBT003 - booleans are not numbers
    with pytest.raises(TypeError):
        js.as_number("1")


def test_json_number() -> None:
    assert js.json_number(5.0) == 5
    assert isinstance(js.json_number(5.0), int)
    assert js.json_number(math.nan) is None
    assert js.json_number(math.inf) is None
    assert json.dumps(js.json_number(2.5)) == "2.5"
    assert str(js.json_number(1e21)) == "1e+21"


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("", 0.0),
        ("  5\n", 5.0),
        ("\u00a07\u3000", 7.0),
        ("0x1F", 31.0),
        ("0o17", 15.0),
        ("0b101", 5.0),
        ("0b102", math.nan),
        ("1e2", 100.0),
        (".5", 0.5),
        ("5.", 5.0),
        ("+3", 3.0),
        ("Infinity", math.inf),
        ("+Infinity", math.inf),
        ("-Infinity", -math.inf),
        ("abc", math.nan),
        ("-0x10", math.nan),
        ("1_000", math.nan),
    ],
)
def test_js_to_number(text: str, number: float) -> None:
    result = js.js_to_number(text)
    assert (math.isnan(result) and math.isnan(number)) or result == number


def test_timer_seconds_like_set_timeout() -> None:
    assert js.timer_seconds(0.5) == 0.5
    assert js.timer_seconds(0) == 0.001
    assert js.timer_seconds(-1) == 0.001
    assert js.timer_seconds(math.nan) == 0.001
    assert js.timer_seconds(math.inf) == 0.001
    assert js.timer_seconds(3e6) == 0.001  # beyond 2^31-1 ms


def test_js_string_and_truthiness() -> None:
    assert js.js_string(None) == "null"
    assert js.js_string(value=True) == "true"
    assert js.js_string(value=False) == "false"
    assert js.js_string(3.0) == "3"
    assert js.js_string([1, None, "a", [2, 3]]) == "1,,a,2,3"
    assert js.js_string({"a": 1}) == "[object Object]"
    truthy: list[Any] = [True, 1, -0.5, "x", [], {}]
    falsy: list[Any] = [None, False, 0, 0.0, "", math.nan]
    assert all(js.js_truthy(v) for v in truthy)
    assert not any(js.js_truthy(v) for v in falsy)


def test_js_stringify_like_json_stringify() -> None:
    value: Any = {
        "a": [1, 2.0, None],
        "b": {},
        "c": [],
        "d": 'q"\\\b\f\n\r\t\u0001\ud800é',
        "e": True,
    }
    assert js.js_stringify(value) == (
        '{"a":[1,2,null],"b":{},"c":[],"d":"q\\"\\\\\\b\\f\\n\\r\\t\\u0001\\ud800é","e":true}'
    )
    assert js.js_stringify({"a": [1, {"b": False}]}, 2) == (
        '{\n  "a": [\n    1,\n    {\n      "b": false\n    }\n  ]\n}'
    )
    assert js.js_stringify(math.inf) == "null"


def test_times() -> None:
    assert js.time_of_day(1792290542000000000) == "10:29:02 PM"  # 02:29 UTC, EDT
    assert js.time_of_day(1792245600000000000) == "10:00:00 AM"
    assert js.time_of_day(1792252800000000000) == "12:00:00 PM"
    assert js.time_of_day(1792209600000000000) == "12:00:00 AM"
    assert js.iso_ms(1792290968191000000) == "2026-10-18T02:36:08.191Z"


@pytest.mark.parametrize(
    ("text", "href"),
    [
        ("demo://resource/session/my file.gz", "demo://resource/session/my%20file.gz"),
        ("DEMO://resource/dynamic/text/../text/1", "demo://resource/dynamic/text/1"),
        ("demo://resource/dynamic/text/./1", "demo://resource/dynamic/text/1"),
        ("demo://resource/a/%2e%2E/b", "demo://resource/b"),
        ("demo://resource/a/..", "demo://resource/"),
        ("demo://resource/a/.", "demo://resource/a/"),
        ("demo://user@host:81/x?q a#f g", "demo://user@host:81/x?q%20a#f%20g"),
        ("demo://host", "demo://host"),
        ("data:text/plain,a b", "data:text/plain,a b"),
        ("mailto:x\x01y\x01", "mailto:x%01y"),
        ("  https://Example.COM:443/a/./b/../c  ", "https://example.com/a/c"),
        ("http:\\\\example.com\\x", "http://example.com/x"),
        ("http://u:p@example.com:8080", "http://u:p@example.com:8080/"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("file:///tmp/a b", "file:///tmp/a%20b"),
        ("ws://h/a?b#c", "ws://h/a?b#c"),
    ],
)
def test_parse_url_serializes_like_whatwg(text: str, href: str) -> None:
    parsed = js.parse_url(text)
    assert parsed is not None
    assert parsed.href == href


@pytest.mark.parametrize(
    "text",
    [
        "not a url",
        "1http://x",
        "http://",
        "http://h:port/",
        "http://h:70000/",
        "http://a b/",
        "demo://a b/",
        "demo://h:x/",
        "https://\u0000/",
        ":nothing",
    ],
)
def test_parse_url_refuses_what_whatwg_refuses(text: str) -> None:
    assert js.parse_url(text) is None


def test_parsed_url_parts() -> None:
    parsed = js.parse_url("https://Example.com/x")
    assert parsed is not None
    assert (parsed.scheme, parsed.protocol, parsed.hostname) == ("https", "https:", "example.com")


# -- zod -------------------------------------------------------------------------------------------


def test_zod_issue_json_and_messages() -> None:
    issue = zod.wrong_type("string", zod.MISSING, ["params", "uri"])
    assert issue.as_json() == {
        "expected": "string",
        "code": "invalid_type",
        "path": ["params", "uri"],
        "message": "Invalid input: expected string, received undefined",
    }
    assert repr(zod.MISSING) == "undefined"
    assert issue.nested(["outer"]).path == ["outer", "params", "uri"]
    assert zod.dot_path(["a", 0, "b"]) == "a[0].b"
    assert zod.tool_message("t", [zod.Issue({}, [], "Plain")]) == (
        "MCP error -32602: Input validation error: Invalid arguments for tool t: Plain"
    )
    samples: tuple[Any, ...] = (None, True, 1, "s", [], {})
    assert [zod.received(v) for v in samples] == [
        "null",
        "boolean",
        "number",
        "string",
        "array",
        "object",
    ]


def test_zod_schemas() -> None:
    schema = zod.ToolSchema(
        "t",
        {
            "n": zod.Field(zod.number(minimum=1, maximum=10), default=3),
            "u": zod.Field(zod.string(url=True), optional=True),
            "b": zod.Field(zod.boolean(), default=False),
        },
    )
    assert schema.parse({}) == ({"n": 3, "b": False}, None)
    assert schema.parse({"u": " https://x.y/\n "})[0]["u"] == "https://x.y/"
    _, refusal = schema.parse({"n": 0, "u": "nope", "b": "yes"})
    assert refusal == (
        "MCP error -32602: Input validation error: Invalid arguments for tool t: "
        "Too small: expected number to be >=1 at n\nInvalid URL at u\n"
        "Invalid input: expected boolean, received string at b"
    )
    assert schema.parse({"n": 11})[1] == (
        "MCP error -32602: Input validation error: Invalid arguments for tool t: "
        "Too big: expected number to be <=10 at n"
    )
    assert schema.parse({"u": 5})[1] is not None
    assert (
        zod.parse_object({}, "x")[1][0].message == "Invalid input: expected object, received string"
    )


# -- client answers --------------------------------------------------------------------------------


def _issues(error: pytest.ExceptionInfo[validation.InvalidAnswerError]) -> Any:
    return json.loads(str(error.value))


def test_create_message_result_order_and_refusals() -> None:
    answer: Any = {
        "extra": 1,
        "content": {"type": "text", "text": "hi", "junk": 2},
        "role": "assistant",
        "model": "m",
        "stopReason": "endTurn",
        "_meta": {},
    }
    parsed = validation.create_message_result(answer)
    assert list(parsed) == ["_meta", "model", "stopReason", "role", "content", "extra"]
    assert parsed["content"] == {"type": "text", "text": "hi"}
    image = validation.create_message_result(
        {
            "model": "m",
            "role": "user",
            "content": {"type": "image", "data": "AA==", "mimeType": "image/png"},
        }
    )
    assert image["content"] == {"type": "image", "data": "AA==", "mimeType": "image/png"}
    with pytest.raises(validation.InvalidAnswerError) as wrong:
        validation.create_message_result(
            {"model": 1, "role": "bot", "stopReason": 5, "content": "x"}
        )
    assert [i["code"] for i in _issues(wrong)] == [
        "invalid_type",
        "invalid_union",
        "invalid_value",
        "invalid_type",
    ]
    with pytest.raises(validation.InvalidAnswerError) as kind:
        validation.create_message_result(
            {"model": "m", "role": "user", "content": {"type": "video"}}
        )
    assert _issues(kind)[0]["path"] == ["content", "type"]
    with pytest.raises(validation.InvalidAnswerError) as missing:
        validation.create_message_result(
            {"model": "m", "role": "user", "content": {"type": "audio"}}
        )
    assert [i["path"] for i in _issues(missing)] == [["content", "data"], ["content", "mimeType"]]
    with pytest.raises(validation.InvalidAnswerError):
        validation.create_message_result([])


def test_elicit_result() -> None:
    assert validation.elicit_result({"action": "accept", "content": None}) == {"action": "accept"}
    assert validation.elicit_result({"x": 1, "action": "decline"}) == {"action": "decline", "x": 1}
    with pytest.raises(validation.InvalidAnswerError) as wrong:
        validation.elicit_result({"action": "maybe", "content": {"a": {"b": 1}, "c": [1]}})
    assert [i["code"] for i in _issues(wrong)] == [
        "invalid_value",
        "invalid_union",
        "invalid_union",
    ]
    with pytest.raises(validation.InvalidAnswerError):
        validation.elicit_result({"action": "accept", "content": "x"})
    with pytest.raises(validation.InvalidAnswerError):
        validation.elicit_result(None)


def test_list_roots_result() -> None:
    assert validation.list_roots_result({"roots": [{"uri": "file:///a", "name": "A", "z": 1}]}) == [
        {"uri": "file:///a", "name": "A"}
    ]
    with pytest.raises(validation.InvalidAnswerError) as wrong:
        validation.list_roots_result(
            {"roots": [1, {"uri": 2}, {"uri": "http://x"}, {"uri": "file:///", "name": 3}]}
        )
    assert [i["path"] for i in _issues(wrong)] == [
        ["roots", 0],
        ["roots", 1, "uri"],
        ["roots", 2, "uri"],
        ["roots", 3, "name"],
    ]
    with pytest.raises(validation.InvalidAnswerError):
        validation.list_roots_result({"roots": "x"})
    with pytest.raises(validation.InvalidAnswerError):
        validation.list_roots_result("x")


def test_task_or_direct_and_polls() -> None:
    fields = (("action", "string", True), ("content", "any", False))
    assert validation.task_or({"task": {"taskId": "t", "status": "working", "x": 1}}, fields) == {
        "task": {"taskId": "t", "status": "working"}
    }
    assert validation.task_or({"action": "accept", "other": 1}, fields) == {"action": "accept"}
    odd: list[Any] = [{"action": 5}, {"content": {}}, "x", {"task": {"taskId": 1}}]
    for bad in odd:
        with pytest.raises(validation.InvalidAnswerError):
            validation.task_or(bad, fields)
    assert validation.poll_result({"status": "working", "x": 1}) == {"status": "working", "x": 1}
    odd_polls: list[Any] = [{"status": 1}, {"status": "w", "statusMessage": 2}, []]
    for bad_poll in odd_polls:
        with pytest.raises(validation.InvalidAnswerError):
            validation.poll_result(bad_poll)
