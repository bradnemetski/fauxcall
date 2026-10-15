"""The front's answers in a session: SSE as the SDK writes it, what the SDK refuses before a
handler runs, sessions ending, the journal, and strings Python can't write.

Recorded: 000218, 000460 (the session id on every SSE answer), 000509/000510 (priming follows the
asked version), 000514-000517 (an ``initialize`` inside a session), 000520-000525 (related
tasks). From the SDK's source: priming by the request's header, and DELETE ending GET streams.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Final

import pytest
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything import surrogates
from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx2
    from fastapi import FastAPI
    from fauxcall.types import JsonObject, JsonValue

URL: Final = "http://fake.test/mcp"
POSTING: Final = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _init(version: str = "2025-06-18") -> JsonObject:
    client: JsonObject = {"name": "probe", "version": "1.0.0"}
    params: JsonObject = {"protocolVersion": version, "capabilities": {}, "clientInfo": client}
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}


def _request(method: str, request_id: int = 2, **params: JsonValue) -> JsonObject:
    message: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        message["params"] = dict(params)
    return message


def _events(response: httpx2.Response) -> list[list[str]]:
    """Each SSE event's lines."""
    return [block.split("\n") for block in response.text.split("\n\n") if block]


def _primed(response: httpx2.Response) -> bool:
    first = _events(response)[0]
    return first[0].startswith("id: ") and first[1:] == ["data: "]


def _replies(response: httpx2.Response) -> list[JsonValue]:
    return [
        json.loads(line[6:])
        for event in _events(response)
        for line in event
        if line.startswith("data: ") and line[6:]
    ]


@pytest.fixture
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with streaming_client(app) as client:
        yield client


async def _session(http: httpx2.AsyncClient, version: str = "2025-06-18") -> dict[str, str]:
    opened = await http.post(URL, json=_init(version), headers=POSTING)
    return {**POSTING, "mcp-session-id": opened.headers["mcp-session-id"]}


# -- SSE as the SDK writes it ----------------------------------------------------------------------


async def test_every_sse_answer_names_the_session(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    pinged = await http.post(URL, json=_request("ping"), headers=headers)
    assert pinged.headers["mcp-session-id"] == headers["mcp-session-id"]
    ((event, event_id, data),) = _events(pinged)  # event first, as writeSSEEvent writes it
    assert (event, data) == ("event: message", 'data: {"result":{},"jsonrpc":"2.0","id":2}')
    assert event_id.startswith("id: ")
    async with http.stream("GET", URL, headers={**headers, "accept": "text/event-stream"}) as got:
        assert got.headers["mcp-session-id"] == headers["mcp-session-id"]


@pytest.mark.parametrize(
    ("asked", "primed"),
    [("2025-11-25", True), ("2099-01-01", True), ("2025-06-18", False), ("1999-01-01", False)],
)
async def test_a_handshake_primes_by_the_version_asked(
    http: httpx2.AsyncClient, asked: str, *, primed: bool
) -> None:
    opened = await http.post(URL, json=_init(asked), headers=POSTING)
    assert _primed(opened) is primed  # 000509 primes, 000510 doesn't, both negotiate 2025-11-25


@pytest.mark.parametrize(
    ("header", "primed"), [(None, False), ("2025-06-18", False), ("2025-11-25", True)]
)
async def test_a_request_primes_by_its_version_header(
    http: httpx2.AsyncClient, header: str | None, *, primed: bool
) -> None:
    headers = await _session(http, "2025-11-25")  # negotiated 2025-11-25 either way
    if header is not None:
        headers["mcp-protocol-version"] = header
    pinged = await http.post(URL, json=_request("ping"), headers=headers)
    assert _primed(pinged) is primed


async def test_a_batch_gets_every_answer(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    batch = [_request("ping", request_id=n) for n in range(100)]
    answered = await http.post(URL, json=batch, headers=headers)
    assert sorted(dig_id(reply) for reply in _replies(answered)) == list(range(100))


def dig_id(reply: JsonValue) -> int:
    assert isinstance(reply, dict)
    assert reply.get("result") == {}
    request_id = reply["id"]
    assert isinstance(request_id, int)
    return request_id


# -- what the SDK refuses before a handler runs ----------------------------------------------------


async def test_a_related_task_is_looked_up_for_every_method(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    related: JsonObject = {"io.modelcontextprotocol/related-task": {"taskId": "nope"}}
    for method in ("ping", "tools/list", "prompts/list", "resources/templates/list"):
        refused = await http.post(URL, json=_request(method, _meta=related), headers=headers)
        assert _replies(refused) == [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32602, "message": "MCP error -32602: Task not found: nope"},
            }
        ]
    unknown = await http.post(URL, json=_request("nope/nope", _meta=related), headers=headers)
    assert _replies(unknown) == [  # 000524: no handler, no lookup
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "Method not found"}}
    ]


async def test_an_initialize_inside_a_session_is_a_request(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    refused = await http.post(URL, json=_request("initialize", capabilities={}), headers=headers)
    assert refused.status_code == 200  # parsed by its handler, not refused by the transport
    (reply,) = _replies(refused)
    assert isinstance(reply, dict)
    assert reply["error"] == {
        "code": -32603,
        "message": json.dumps(
            [
                {
                    "expected": "string",
                    "code": "invalid_type",
                    "path": ["params", "protocolVersion"],
                    "message": "Invalid input: expected string, received undefined",
                },
                {
                    "expected": "object",
                    "code": "invalid_type",
                    "path": ["params", "clientInfo"],
                    "message": "Invalid input: expected object, received undefined",
                },
            ],
            indent=2,
        ),
    }


# -- sessions ending, and the journal ------------------------------------------------------------


async def test_a_delete_ends_the_sessions_stream(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    listening = {**headers, "accept": "text/event-stream"}
    async with http.stream("GET", URL, headers=listening) as stream:
        assert stream.status_code == 200
        reading = asyncio.ensure_future(stream.aread())
        await asyncio.sleep(0.05)
        assert not reading.done()
        assert (await http.delete(URL, headers=headers)).status_code == 200
        await asyncio.wait_for(reading, 1)  # the transport's close() ends its streams
    again = await http.get(URL, headers=listening)
    assert again.status_code == 400


async def test_the_fronts_answers_are_journaled(app: FastAPI, http: httpx2.AsyncClient) -> None:
    runtime = runtime_of_app(app)
    runtime.reset(journal=True)
    await http.get("http://fake.test/.well-known/oauth-protected-resource")
    await http.options(URL)
    await http.post(URL, content=b"{", headers={**POSTING, "accept": "text/plain"})
    headers = await _session(http)
    related: JsonObject = {"io.modelcontextprotocol/related-task": {"taskId": "t"}}
    await http.post(URL, json=_request("ping", _meta=related), headers=headers)
    entries = runtime.journal.query()
    noted = [(e.method, e.path, e.status, e.mcp_method) for e in entries]
    assert noted[:3] == [
        ("GET", "/.well-known/oauth-protected-resource", 404, None),
        ("OPTIONS", "/mcp", 204, None),
        ("POST", "/mcp", 406, None),
    ]
    assert noted[-1] == ("POST", "/mcp", 200, "ping")
    assert entries[0].response_text is not None
    assert "Cannot GET /.well-known/oauth-protected-resource" in entries[0].response_text
    assert entries[-1].endpoint == "MCP ping"
    assert entries[-1].response_body == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32602, "message": "MCP error -32602: Task not found: t"},
    }


# -- strings Python can't write --------------------------------------------------------------------


async def test_a_lone_surrogate_is_echoed_escaped(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    call = (
        b'{"jsonrpc":"2.0","id":3,"method":"tools/call",'
        b'"params":{"name":"echo","arguments":{"message":"\\ud83d|\\ud83d\\ude00|\\\\ud800"}}}'
    )
    echoed = await http.post(URL, content=call, headers=headers)
    assert echoed.status_code == 200
    data = next(line for line in echoed.text.splitlines() if line.startswith("data: {"))
    assert '"text":"Echo: \\ud83d|\U0001f600|\\\\ud800"' in data


async def test_bad_bytes_and_deep_nesting(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    bad = (
        b'{"jsonrpc":"2.0","id":3,"method":"tools/call",'
        b'"params":{"name":"echo","arguments":{"message":"a\xff"}}}'
    )
    echoed = await http.post(URL, content=bad, headers=headers)
    assert "Echo: a\ufffd" in echoed.text  # TextDecoder replaces what isn't UTF-8
    deep = b'{"jsonrpc":"2.0","id":4,"method":"ping","params":{"x":' + b"[" * 50_000
    deep += b"]" * 50_000 + b"}}"
    refused = await http.post(URL, content=deep, headers=headers)
    assert refused.status_code == 400  # deeper than Python parses: refused, not a crash


@pytest.mark.parametrize(
    ("text", "hidden"),
    [
        ('"\\ud800"', '"\U000f0000"'),
        ('"\\udfff x"', '"\U000f07ff x"'),
        ('"\\ud83d\\ude00"', '"\\ud83d\\ude00"'),  # a pair is a character
        ('"\\\\ud800"', '"\\\\ud800"'),  # an escaped backslash, then text
        ('"\\ude00\\ud83d"', '"\U000f0600\U000f003d"'),  # the wrong way round: both lone
        ('"\\u0041"', '"\\u0041"'),
    ],
)
def test_lone_surrogates_get_stand_ins(text: str, hidden: str) -> None:
    assert surrogates.hide(text) == hidden
    assert surrogates.reveal(hidden.encode()).decode() == text
