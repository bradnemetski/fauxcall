"""The JavaScript, WHATWG URL and zod behavior the handlers reproduce."""

from __future__ import annotations

import math
from typing import Any

import pytest

from fauxcall_everything import jsfmt, whatwg, zod


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.1 + 0.2, "0.30000000000000004"),
        (1e21, "1e+21"),
        (123e19, "1.23e+21"),
        (1e-7, "1e-7"),
        (0.000001, "0.000001"),
        (2.0, "2"),
        (-0.0, "0"),
        (math.inf, "Infinity"),
        (-math.inf, "-Infinity"),
        (math.nan, "NaN"),
        (2**60, "1152921504606847000"),
        (-1.5, "-1.5"),
        (123.456, "123.456"),
    ],
)
def test_number_text(value: float, text: str) -> None:
    assert jsfmt.number_text(jsfmt.to_float(value)) == text


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("", 0.0),
        ("  42\n", 42.0),
        ("0x1f", 31.0),
        ("0O17", 15.0),
        ("0b101", 5.0),
        ("0x", math.nan),
        ("0xZ", math.nan),
        ("0b2", math.nan),
        ("+0x10", math.nan),
        ("Infinity", math.inf),
        ("+Infinity", math.inf),
        ("-Infinity", -math.inf),
        ("1e3", 1000.0),
        (".5", 0.5),
        ("5.", 5.0),
        ("1_000", math.nan),
        ("abc", math.nan),
    ],
)
def test_to_number(text: str, value: float) -> None:
    result = jsfmt.to_number(text)
    assert (math.isnan(result) and math.isnan(value)) or result == value


def test_to_float() -> None:
    assert jsfmt.to_float(True) == 1.0  # noqa: FBT003
    assert jsfmt.to_float(10**400) == math.inf
    assert jsfmt.to_float(-(10**400)) == -math.inf
    with pytest.raises(TypeError):
        jsfmt.to_float("1")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (jsfmt.UNDEFINED, "undefined"),
        (None, "null"),
        (True, "true"),
        (False, "false"),
        (3.0, "3"),
        ("s", "s"),
        (["a", None, 1, [2, 3]], "a,,1,2,3"),
        ({"a": 1}, "[object Object]"),
    ],
)
def test_js_string(value: Any, text: str) -> None:
    assert jsfmt.js_string(value) == text


def test_stringify_like_json_stringify() -> None:
    value: Any = {"a": [1, 2.5, None], "b": {}, "c": [], "u": jsfmt.UNDEFINED, "n": math.nan}
    assert jsfmt.stringify(value) == (
        '{\n  "a": [\n    1,\n    2.5,\n    null\n  ],\n  "b": {},\n  "c": [],\n  "n": null\n}'
    )
    assert jsfmt.stringify({"x": "é\n"}, 0) == '{"x":"é\\n"}'
    assert jsfmt.stringify(jsfmt.UNDEFINED) == "null"
    only_undefined: Any = {"u": jsfmt.UNDEFINED}
    assert jsfmt.stringify(only_undefined) == "{}"
    assert repr(jsfmt.UNDEFINED) == "undefined"


def test_json_number() -> None:
    assert jsfmt.json_number(2.0) == 2
    assert isinstance(jsfmt.json_number(2.0), int)
    assert jsfmt.json_number(1e21).lexeme == "1e+21"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("text", "href"),
    [
        ("demo://resource/session/My File.gz", "demo://resource/session/My%20File.gz"),
        ("HTTPS://Example.COM:443/a/./b/../c?q=a b#f g", "https://example.com/a/c?q=a%20b#f%20g"),
        ("http://user:pa ss@host:8080/p", "http://user:pa%20ss@host:8080/p"),
        ("http://0x7f.1/", "http://127.0.0.1/"),
        ("http://017.0.0.1/", "http://15.0.0.1/"),
        ("http://[::FFFF:1.2.3.4]/", "http://[::ffff:102:304]/"),
        ("http://bücher.de/", "http://xn--bcher-kva.de/"),
        ("http:\\\\host\\path\\x", "http://host/path/x"),
        ("file://localhost/etc/hosts", "file:///etc/hosts"),
        ("file://server/share", "file://server/share"),
        ("demo:/a/./b/%2e%2e/c", "demo:/a/c"),
        ("demo://h/a/..", "demo://h/"),
        ("demo://h/a/.", "demo://h/a/"),
        ("demo://h", "demo://h"),
        ("demo:opaque path é", "demo:opaque path %C3%A9"),
        ("demo://[::1]/x", "demo://[::1]/x"),
        ("ws://h:80/", "ws://h/"),
        ("demo://h:/x", "demo://h/x"),
    ],
)
def test_href(text: str, href: str) -> None:
    assert whatwg.href(text) == href


@pytest.mark.parametrize(
    "text",
    [
        "no scheme",
        "1http://x",
        "http://",
        "http://exa mple.com/",
        "http://1.2.3.4.5/",
        "http://256.0.0.1/",
        "http://1.2.3.4294967296/",
        "http://[::1/",
        "http://[zz]/",
        "http://%ff/",
        "http://h:99999/",
        "http://h:8x/",
        "demo://a b/",
        "demo://x^y/",
        "http://.%2541/",
        "http://xn--a.com\u0000/",
    ],
)
def test_invalid_urls(text: str) -> None:
    assert not whatwg.is_valid_url(text)
    with pytest.raises(whatwg.InvalidUrlError):
        whatwg.href(text)


def test_zod_messages() -> None:
    assert zod.invalid_value(["only"], ["k"]).message == 'Invalid input: expected "only"'
    assert zod.dot_path(["a", 0, "b"]) == "a[0].b"
    issue = zod.Issue({"code": "custom", "message": "m"})
    assert zod.sdk_message([issue]) == "m"
    assert zod.received(math.nan) == "nan"
    assert zod.received(jsfmt.UNDEFINED) == "undefined"


def test_zod_parse() -> None:
    fields = [
        zod.Field("n", "number", minimum=0, maximum=1),
        zod.Field("u", "url"),
        zod.Field("o", "string", optional=True),
    ]
    data, issues = zod.parse(fields, {"n": math.inf, "u": " https://x.y/\t "})
    assert data == {"n": math.inf, "u": "https://x.y/"}
    assert [issue.message for issue in issues] == [
        "Invalid input: expected number, received number",
        "Too big: expected number to be <=1",
    ]
    _, missing = zod.parse(fields, {"n": -1})
    assert [issue.message for issue in missing] == [
        "Too small: expected number to be >=0",
        "Invalid input: expected string, received undefined",
    ]
