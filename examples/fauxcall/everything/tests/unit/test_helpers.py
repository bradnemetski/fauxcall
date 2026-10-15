"""The JavaScript, URL and zod emulation the handlers rely on (cases from recordings and Node)."""

from __future__ import annotations

import base64
import gzip
import math

import pytest
from fauxcall.runtime import RpcError
from mcp_session import dig

from fauxcall_everything import jsvalues, webdata, zodish
from fauxcall_everything.zodish import Field


@pytest.mark.parametrize(
    ("value", "spelled"),
    [
        (0.1 + 0.2, "0.30000000000000004"),  # 000055
        (1e21, "1e+21"),  # 000058
        (2e308, "Infinity"),
        (-2e308, "-Infinity"),
        (math.nan, "NaN"),
        (1e-7, "1e-7"),  # 000060
        (9007199254740993, "9007199254740992"),  # 000061
        (-0.0, "0"),
        (1e20, "100000000000000000000"),  # 000378
        (12345600 - 0.000001, "12345599.999999"),  # 000389
        (1.7976931348623157e308 - 1e292, "1.7976931348623155e+308"),  # 000390
        (0.000001, "0.000001"),
        (-1.5, "-1.5"),
        (10**400, "Infinity"),
    ],
)
def test_number_to_string(value: float, spelled: str) -> None:
    assert jsvalues.number_to_string(value) == spelled


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("1e3", 1000.0),  # 000374
        ("0x10", 16.0),  # 000375
        ("0b11", 3.0),  # 000382
        ("0o17", 15.0),
        ("5.0", 5.0),
        ("+5", 5.0),
        ("", 0.0),
        ("  7 ", 7.0),
        ("-Infinity", -math.inf),
        ("+Infinity", math.inf),
    ],
)
def test_parse_number(text: str, number: float) -> None:
    assert jsvalues.parse_number(text) == number


@pytest.mark.parametrize("text", ["abc", "0x", "0xg", "-0x10", "1_000", "3?x=1", "%31", "1e"])
def test_parse_number_nan(text: str) -> None:
    assert math.isnan(jsvalues.parse_number(text))


def test_template_and_truthiness() -> None:
    assert jsvalues.template(value=True) == "true"
    assert jsvalues.template(None) == "null"
    assert jsvalues.template([1, None, "a", [2]]) == "1,,a,2"
    assert jsvalues.template({"a": 1}) == "[object Object]"
    assert jsvalues.template(2.0) == "2"
    assert not jsvalues.truthy("")
    assert not jsvalues.truthy(0)
    assert not jsvalues.truthy(math.nan)
    assert not jsvalues.truthy(None)
    assert jsvalues.truthy({})
    assert jsvalues.truthy([])
    assert jsvalues.template(value=False) == "false"
    assert jsvalues.truthy("x")
    assert jsvalues.json_number(2.0) == 2
    assert isinstance(jsvalues.json_number(2.0), int)
    assert jsvalues.json_number(2.5) == 2.5


def test_stringify_like_json_stringify() -> None:
    assert jsvalues.stringify({"a": [1.0, None, True], "b": {}}) == '{"a":[1,null,true],"b":{}}'
    assert jsvalues.stringify({"x": math.inf, "e": []}, 2) == '{\n  "x": null,\n  "e": []\n}'
    assert jsvalues.stringify("é\n") == '"é\\n"'
    assert jsvalues.stringify([], 2) == "[]"


def test_type_names() -> None:
    values: list[object] = [None, True, 1, "s", [], {}]
    names = [jsvalues.type_name(v) for v in values]
    assert names == ["null", "boolean", "number", "string", "array", "object"]
    assert jsvalues.type_name(None, present=False) == "undefined"


def test_data_urls() -> None:
    assert webdata.data_url_body("data:text/plain;base64,SGVsbG8sIFdvcmxkIQ==") == b"Hello, World!"
    assert webdata.data_url_body("data:,a%20b%zz#fragment") == b"a b%zz"
    assert webdata.data_url_body("data:;BASE64 ,  SGk =") == b"Hi"  # media type is trimmed
    assert webdata.data_url_body("data:;base64,SGk") == b"Hi"
    assert webdata.data_url_body("data:;base64,S G k = ") == b"Hi"
    assert webdata.data_url_body("data:;base64,SGk==") is None
    assert webdata.data_url_body("data:;base64,S") is None
    assert webdata.data_url_body("data:;base64,SG*k") is None
    assert webdata.data_url_body("data:no-comma") is None
    assert webdata.data_url_body("https://example.com/") is None
    assert webdata.data_url_body("not a url") is None


def test_gzip_and_urls() -> None:
    packed = webdata.node_gzip(b"payload")
    assert packed[9] == webdata.GZIP_OS_MACOS
    assert gzip.decompress(packed) == b"payload"
    # recorded 000198: data:text/plain;base64,SGVsbG8sIFdvcmxkIQ==
    assert base64.b64encode(webdata.node_gzip(b"Hello, World!")).decode() == (
        "H4sIAAAAAAAAE/NIzcnJ11EIzy/KSVEEANDDSuwNAAAA"
    )
    assert webdata.href("https://Example.com") == "https://example.com/"
    assert webdata.href("demo://x/ a") == "demo://x/%20a"
    assert webdata.href("not a uri") is None
    assert webdata.protocol("DATA:,x") == "data:"


def test_tool_arguments() -> None:
    fields = (
        Field("url", "url"),
        Field("count", "number", minimum=1, maximum=10),
        Field("flag", "boolean", optional=True),
        Field("kind", "enum", values=("a", "b")),
    )
    parsed, problems = zodish.parse_arguments(fields, {"url": " https://x.io/a\tb ", "count": 3})
    assert parsed == {"url": "https://x.io/ab", "count": 3}
    assert problems == ['Invalid option: expected one of "a"|"b" at kind']
    _, problems = zodish.parse_arguments(fields, {"url": 5, "count": 11, "flag": "y", "kind": 1})
    assert problems == [
        "Invalid input: expected string, received number at url",
        "Too big: expected number to be <=10 at count",
        "Invalid input: expected boolean, received string at flag",
        'Invalid option: expected one of "a"|"b" at kind',
    ]
    _, problems = zodish.parse_arguments(fields, {"count": 0.5, "kind": "a"})
    assert problems == [
        "Invalid input: expected string, received undefined at url",
        "Too small: expected number to be >=1 at count",
    ]
    _, problems = zodish.parse_arguments(fields, {"url": "not a url", "count": 1, "kind": "b"})
    assert problems == ["Invalid URL at url"]


def test_protocol_refusals() -> None:
    assert zodish.require_string({"uri": "x"}, "uri") == "x"
    with pytest.raises(RpcError) as refused:
        zodish.require_string({}, "uri")
    assert refused.value.code == zodish.INTERNAL_ERROR
    assert '"Invalid input: expected string, received undefined"' in refused.value.message


def test_sampling_results() -> None:
    parsed, issues = zodish.create_message_result(
        {
            "x": 1,
            "content": {
                "text": "t",
                "type": "text",
                "annotations": {"priority": 1, "audience": []},
            },
            "role": "user",
            "model": "m",
        }
    )
    assert issues == []
    assert list(parsed) == ["model", "role", "content", "x"]
    assert parsed["content"] == {
        "type": "text",
        "text": "t",
        "annotations": {"audience": [], "priority": 1},
    }
    _, issues = zodish.create_message_result({"model": "m", "role": "user", "content": "text"})
    assert issues[0]["code"] == "invalid_type"
    assert issues[0]["expected"] == "object"
    _, issues = zodish.create_message_result(
        {"model": "m", "role": "user", "content": {"type": "image", "data": "AA"}}
    )
    assert issues[0]["path"] == ["content", "mimeType"]
    _, issues = zodish.create_message_result("nonsense")
    assert [i["path"] for i in issues] == [["model"], ["role"], ["content"]]


def test_elicitation_results() -> None:
    parsed, issues = zodish.elicit_result({"zz": 1, "content": {"tags": ["a"]}, "action": "accept"})
    assert issues == []
    assert list(parsed) == ["action", "content", "zz"]
    _, issues = zodish.elicit_result({"action": "accept", "content": [1]})
    assert issues[0]["message"] == "Invalid input: expected record, received array"
    _, issues = zodish.elicit_result({"action": "accept", "content": {"tags": ["a", 2]}})
    assert issues[0]["code"] == "invalid_union"
    assert dig(issues[0], "errors", 3) == [zodish.type_issue("string", 2, [1])]
    _, issues = zodish.elicit_result(None)
    assert issues[0]["path"] == ["action"]


def test_roots_results() -> None:
    assert zodish.roots_result({"roots": [{"name": "n", "uri": "file:///a", "x": 1}]}) == [
        {"uri": "file:///a", "name": "n"}
    ]
    assert zodish.roots_result({"roots": "x"}) is None
    assert zodish.roots_result({"roots": ["x"]}) is None
    assert zodish.roots_result({"roots": [{"uri": "file:///a", "name": 5}]}) is None
    assert zodish.roots_result({"roots": [{"uri": "https://a"}]}) is None
