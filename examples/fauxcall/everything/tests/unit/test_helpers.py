"""The fake's pure helpers, checked against what the real server printed (recording ids noted)."""

from __future__ import annotations

import asyncio
import base64
import gzip
import math
from pathlib import Path
from typing import Any

import pytest
from fauxcall.runtime.recordings import RecordingIndex

from fauxcall_everything import handlers, jsvalues, research, sessions, urls, zodlike
from fauxcall_everything.settings import Settings


def anything(values: list[Any]) -> list[Any]:
    """Values of any JSON type, as test inputs."""
    return values


RECORDINGS = RecordingIndex(Path(Settings().data_dir) / "recordings")


@pytest.mark.parametrize(
    ("value", "spelled"),
    [
        (0.30000000000000004, "0.30000000000000004"),
        (1e16, "10000000000000000"),
        (2e21, "2e+21"),
        (6e-7, "6e-7"),
        (0.000001, "0.000001"),
        (-0.0, "0"),
        (1.2345678901234568e29, "1.2345678901234568e+29"),
        (1234.56, "1234.56"),
        (12, "12"),
        (math.nan, "NaN"),
        (math.inf, "Infinity"),
        (-math.inf, "-Infinity"),
        (-2.5e-9, "-2.5e-9"),
    ],
)
def test_numbers_are_spelled_like_javascript(value: float, spelled: str) -> None:
    assert jsvalues.number_text(value) == spelled


def test_plain_and_stringify() -> None:
    value: Any = {"a": 2.0, "b": [1.5, 2**60, None, True], "c": {}, "d": [], "e": "é\n"}
    assert jsvalues.plain(value) == {
        "a": 2,
        "b": [1.5, float(2**60), None, True],
        "c": {},
        "d": [],
        "e": "é\n",
    }
    assert jsvalues.plain(math.inf) == math.inf
    assert (
        jsvalues.stringify(value)
        == '{"a":2,"b":[1.5,1152921504606847000,null,true],"c":{},"d":[],"e":"é\\n"}'
    )
    assert jsvalues.stringify([math.nan, False]) == "[null,false]"
    pretty = jsvalues.stringify({"x": [1, {"y": None}]}, indent=2)
    assert pretty == '{\n  "x": [\n    1,\n    {\n      "y": null\n    }\n  ]\n}'


def test_template_truthiness_and_types() -> None:
    assert [
        jsvalues.template(v)
        for v in anything([None, True, False, 2.0, "s", [1, None, "a"], {"k": 1}])
    ] == [
        "null",
        "true",
        "false",
        "2",
        "s",
        "1,,a",
        "[object Object]",
    ]
    assert [
        jsvalues.truthy(v) for v in anything([None, False, True, 0, 0.5, math.nan, "", "x", [], {}])
    ] == [
        False,
        False,
        True,
        False,
        True,
        False,
        False,
        True,
        True,
        True,
    ]
    assert [jsvalues.type_name(v) for v in anything([None, True, 1, "s", [], {}])] == [
        "null",
        "boolean",
        "number",
        "string",
        "array",
        "object",
    ]


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("007", 7),
        ("1e3", 1000),
        ("", 0),
        ("  5" + chr(0xA0), 5),
        ("0x1F", 31),
        ("0o17", 15),
        ("0b101", 5),
        ("-Infinity", -math.inf),
        (".5", 0.5),
    ],
)
def test_number_of_strings(text: str, number: float) -> None:
    assert jsvalues.number_of(text) == number


def test_number_of_rejects_what_javascript_rejects() -> None:
    for text in ("abc", "-0x10", "1_000", "4?x=1"):
        assert math.isnan(jsvalues.number_of(text))
    assert jsvalues.is_positive_integer(3.0)
    assert not jsvalues.is_positive_integer(math.inf)
    assert not jsvalues.is_positive_integer(1.5)


@pytest.mark.parametrize(
    ("text", "href"),
    [
        (
            "DEMO://resource/static/document/features.md",
            "demo://resource/static/document/features.md",
        ),
        ("demo://resource/session/my file.gz", "demo://resource/session/my%20file.gz"),
        ("demo://resource/dynamic/text/../text/8", "demo://resource/dynamic/text/8"),
        ("demo://resource/a/./b/..", "demo://resource/a/"),
        ("demo://resource/a/%2e%2e/b/.", "demo://resource/b/"),
        (
            "HTTP://User:Pw@Example.COM:80/a/b?q='x'#frag ment",
            "http://User:Pw@example.com/a/b?q=%27x%27#frag%20ment",
        ),
        ("https://example.com:8443", "https://example.com:8443/"),
        ("https://[::1]:3000/x", "https://[::1]:3000/x"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("mailto:someone@example.com", "mailto:someone@example.com"),
        ("demo:///no-host/x", "demo:///no-host/x"),
        ("demo:/just/a/path", "demo:/just/a/path"),
        ("demo://host", "demo://host"),
        ("file:///etc/hosts", "file:///etc/hosts"),
        ("data:text/plain,hello world", "data:text/plain,hello world"),
        ("demo://h/p?a=<b>", "demo://h/p?a=%3Cb%3E"),
    ],
)
def test_urls_parse_like_new_url(text: str, href: str) -> None:
    parsed = urls.parse(text)
    assert parsed is not None
    assert parsed.href == href


@pytest.mark.parametrize(
    "text",
    [
        "not a url",
        "http://",
        "https://exa mple.com",
        "http://host:99999/",
        "http://host:x/",
        "https://[::1/",
        "demo://ho st/",
        "1http://x",
    ],
)
def test_invalid_urls(text: str) -> None:
    assert urls.parse(text) is None
    assert not urls.is_url(text)


def test_data_urls_decode_like_fetch() -> None:
    def body(text: str) -> bytes | None:
        parsed = urls.parse(text)
        assert parsed is not None
        return urls.data_url_bytes(parsed)

    assert body("data:text/plain;base64,SGVsbG8=") == b"Hello"
    assert body("data:;base64,SGVsbG8") == b"Hello"
    assert body("data:text/plain;base64, SGVs bG8= ") == b"Hello"
    assert body("data:text/plain,%E2%9C%93") == "✓".encode()
    assert body("data:,") == b""
    assert body("data:text/plain;base64,@@@") is None
    assert body("data:text/plain;base64,A") is None
    assert body("data:no-comma") is None


def test_node_gzip_matches_the_recorded_bytes() -> None:
    checked = 0
    for exchange in RECORDINGS.for_endpoint("MCP tools/call:gzip-file-as-resource"):
        sent: Any = exchange.request.body.parsed()
        reply: Any = exchange.response.body.parsed() if exchange.response else None
        assert isinstance(sent, dict)
        arguments = sent["params"].get("arguments", {})
        source = str(arguments.get("data", ""))
        result: Any = reply.get("result") if isinstance(reply, dict) else None
        if not source.startswith("data:") or not isinstance(result, dict) or "isError" in result:
            continue
        block = result["content"][0]
        if block.get("type") != "resource":
            continue
        parsed = urls.parse(source)
        assert parsed is not None
        data = urls.data_url_bytes(parsed)
        assert data is not None
        assert base64.b64encode(handlers.node_gzip(data)).decode() == block["resource"]["blob"]
        checked += 1
    assert checked >= 4


def test_step_seconds_follows_node_timers() -> None:
    assert handlers.step_seconds(1, 4) == 0.25
    for duration, steps in ((1, 0), (-1, 2), (1e10, 1), (math.nan, 1), (0.0004, 1), (0, 200)):
        assert handlers.step_seconds(duration, steps) == 0.001  # Node's 1 ms floor (000534)


def test_root_validation() -> None:
    assert handlers._valid_roots({"roots": [{"uri": "file:///a", "name": "A"}]}) == [
        {"uri": "file:///a", "name": "A"}
    ]
    assert handlers._valid_roots({"roots": "none"}) is None
    assert handlers._valid_roots({"roots": [{"uri": "https://a"}]}) is None
    assert handlers._valid_roots({"roots": [{"uri": "file:///a", "name": 3}]}) is None
    assert handlers._valid_roots([]) is None


def test_local_time_of_day() -> None:
    midnight_ny = 1793073600 * 10**9  # 2026-10-27T04:00:00Z
    assert sessions.local_time(midnight_ny) == "12:00:00 AM"
    assert sessions.local_time(midnight_ny + 12 * 3600 * 10**9) == "12:00:00 PM"
    assert sessions.local_time(midnight_ny + (19 * 3600 + 54 * 60 + 20) * 10**9) == "7:54:20 PM"


class _Channel:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, Any]] = []

    def push(self, method: str, params: Any = None) -> bool:
        self.pushed.append((method, params))
        return True

    async def pause(self, seconds: float) -> None:
        del seconds


def test_log_respects_the_session_level() -> None:
    channel = _Channel()
    state: dict[str, Any] = {"log_level": "error"}
    sessions.log(channel, state, "info", "hidden")  # type: ignore[arg-type]
    sessions.log(channel, state, "critical", "shown", logger="srv")  # type: ignore[arg-type]
    assert channel.pushed == [
        ("notifications/message", {"level": "critical", "logger": "srv", "data": "shown"})
    ]
    sessions.log(None, {}, "info", "nowhere")


def test_result_validation() -> None:
    assert sessions.elicit_result("x")[1][0]["expected"] == "object"
    parsed, problems = sessions.elicit_result(
        {"_meta": {}, "action": "accept", "content": None, "extra": 1}
    )
    assert parsed == {"_meta": {}, "action": "accept", "extra": 1}
    assert not problems
    assert (
        sessions.elicit_result({"action": "accept", "content": [1]})[1][0]["expected"] == "record"
    )
    undefined: Any = sessions.sampling_result(None)[1][0]
    assert undefined["message"].endswith("received undefined")
    parsed, problems = sessions.sampling_result(
        {
            "_meta": {"a": 1},
            "stopReason": "x",
            "role": "user",
            "model": "m",
            "content": {
                "type": "audio",
                "data": "AA==",
                "mimeType": "audio/wav",
                "annotations": {},
                "junk": 1,
            },
        }
    )
    assert list(parsed) == ["_meta", "model", "stopReason", "role", "content"]
    assert parsed["content"] == {
        "type": "audio",
        "data": "AA==",
        "mimeType": "audio/wav",
        "annotations": {},
    }
    assert not problems
    assert sessions.sampling_content({"type": "video"})[1][0]["note"] == "No matching discriminator"
    assert sessions.sampling_content({"type": "text"})[1][0]["path"] == ["content", "text"]
    null: Any = sessions.sampling_content(None, present=True)[1][0]
    assert null["message"].endswith("received null")


def test_report_variants_match_the_recordings() -> None:
    expected = {
        "000273": ("quantum computing", None),
        "000299": ("Python", "programming"),
        "000305": ("python packaging", "technical (default - elicitation unavailable)"),
    }
    for exchange_id, (topic, clarification) in expected.items():
        exchange = RECORDINGS.get(exchange_id)
        assert exchange is not None
        reply: Any = exchange.response.body.parsed() if exchange.response else None
        assert isinstance(reply, dict)
        task = research.Task("x", topic, bool(clarification), 0, 0, clarification=clarification)
        assert research.report(task) == {"content": reply["result"]["content"]}


def test_clarifications_and_expiry() -> None:
    assert (
        research._clarification({"action": "accept", "content": {"interpretation": "snake"}})
        == "snake"
    )
    assert (
        research._clarification({"action": "accept", "content": {}})
        == "User accepted without selection"
    )
    clarified: Any = [
        research._clarification({"action": a}) for a in ("decline", "cancel", "maybe")
    ]
    assert clarified[0].startswith("User declined")
    assert clarified[1].startswith("User cancelled")
    assert clarified[2].startswith("technical")
    assert research._clarification({"action": "accept", "content": {"interpretation": 5}}) == 5
    assert research._clarification({"action": "accept", "content": {"interpretation": {}}}) == (
        "technical (default - elicitation unavailable)"
    )
    first: Any = research.interpretations("PYTHON tips")[0]
    assert first["const"] == "programming"
    task = research.Task("x", "t", ambiguous=False, created_ns=0, updated_ns=0)
    assert not task.expired(299 * 10**9)
    assert task.expired(300 * 10**9)
    task.ended_ns = 100 * 10**9
    assert not task.expired(399 * 10**9)
    assert research.iso_ms(1793059507184_000_000) == "2026-10-27T00:05:07.184Z"
    task.message = None
    assert "statusMessage" not in task.as_json()
    numeric = research.Task("x", "n", ambiguous=True, created_ns=0, updated_ns=0, clarification=5)
    with pytest.raises(TypeError, match=r"state\.clarification\.includes is not a function"):
        research.report(numeric)
    listed = research.Task(
        "x", "l", ambiguous=True, created_ns=0, updated_ns=0, clarification=["a", "b"]
    )
    listed_report: Any = research.report(listed)
    assert listed_report["content"][0]["text"].startswith("# Research Report: l (a,b)\n")


async def test_an_unanswered_clarification_falls_back() -> None:
    task = research.Task("x", "jaguar", ambiguous=True, created_ns=0, updated_ns=0)
    assert await research._ask(task, _Channel()) == "technical (default - elicitation unavailable)"  # type: ignore[arg-type]
    assert len(task.queue) == 1  # still queued for the next tasks/result (000523)
    task.queue.clear()

    async def answer_later() -> None:
        for _ in range(10_000):
            if task.queue:
                break
            await asyncio.sleep(0)
        assert task.queue
        task.queue[0][1].set_exception(RuntimeError("refused"))

    class _Slow(_Channel):
        async def pause(self, seconds: float) -> None:
            del seconds
            await asyncio.sleep(1)

    helper = asyncio.ensure_future(answer_later())
    assert await research._ask(task, _Slow()) == "technical (default - elicitation unavailable)"  # type: ignore[arg-type]
    await helper


def test_zod_issue_wording() -> None:
    args = (
        zodlike.Arg("n", "number", minimum=1, maximum=10),
        zodlike.Arg("u", "url", optional=True),
    )
    assert zodlike.check(args, {"n": 0}) == ({}, ["Too small: expected number to be >=1 at n"])
    assert zodlike.check(args, {"n": 11, "u": "no"})[1] == [
        "Too big: expected number to be <=10 at n",
        "Invalid URL at u",
    ]
    assert zodlike.check(args, {"n": 2, "u": " https://a.example/x\n "}) == (
        {"n": 2, "u": "https://a.example/x"},
        [],
    )
    with pytest.raises(Exception, match="expected string, received number"):
        zodlike.require_string({"uri": 3}, "uri")
    assert gzip.decompress(handlers.node_gzip(b"")) == b""
