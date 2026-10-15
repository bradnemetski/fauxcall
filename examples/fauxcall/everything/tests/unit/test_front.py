"""The real server's HTTP front: CORS, Express's 404 page and routing, the transport's refusals.

Recorded where FauxCall could record it (000006, 000013, 000014, 000017-000019, 000448, 000449,
000508-000512); the rest follows the published source of Express 5.3, cors 2.8.6 and the MCP
SDK's Streamable HTTP transport (see ``front.py``).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Final

import pytest
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests
from mcp_session import connect

from fauxcall_everything import express, front, jsonrpc, sse
from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx2
    from fastapi import FastAPI
    from fauxcall.types import JsonObject, JsonValue

URL: Final = "http://fake.test"
POSTING: Final = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}
CLIENT: Final[JsonObject] = {"name": "probe", "version": "1.0.0"}
NO_SESSION_ETAG: Final = 'W/"5f-yGKrXbkLhApmVYswnToQyaFrApg"'
"""Express's ETag of its refusal, as recorded (000017)."""
EXPOSED: Final = "mcp-session-id,last-event-id,mcp-protocol-version"


def _init(version: str = "2025-06-18", /, **params: JsonValue) -> JsonObject:
    sent: JsonObject = {"protocolVersion": version, "capabilities": {}, "clientInfo": CLIENT}
    sent.update(params)
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": sent}


def _client(**extra: JsonValue) -> JsonObject:
    return {**CLIENT, **extra}


def _ping(**params: JsonValue) -> JsonObject:
    message: JsonObject = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    if params:
        message["params"] = dict(params)
    return message


def _sdk_error(code: int, message: str) -> JsonObject:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": None}


def _messages(response: httpx2.Response) -> list[JsonValue]:
    lines = response.text.splitlines()
    return [json.loads(line[6:]) for line in lines if line.startswith("data: ") and line[6:]]


@pytest.fixture
def app() -> FastAPI:
    return build_app(settings_for_tests(Settings))


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with streaming_client(app) as client:
        yield client


async def _session(http: httpx2.AsyncClient) -> dict[str, str]:
    """Headers of a request in a new session."""
    opened = await http.post(f"{URL}/mcp", json=_init(), headers=POSTING)
    assert opened.status_code == 200
    session = opened.headers["mcp-session-id"]
    return {**POSTING, "mcp-session-id": session, "mcp-protocol-version": "2025-06-18"}


def _fronted(response: httpx2.Response) -> None:
    """What Express and cors put on every answer."""
    assert response.headers["x-powered-by"] == "Express"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-expose-headers"] == EXPOSED


# -- answers that pass through -------------------------------------------------------------------


async def test_answers_carry_the_express_cors_and_sse_headers(http: httpx2.AsyncClient) -> None:
    opened = await http.post(f"{URL}/mcp", json=_init(), headers=POSTING)
    _fronted(opened)
    assert opened.headers["content-type"] == "text/event-stream"  # 000001: no charset
    assert opened.headers["cache-control"] == "no-cache, no-transform"
    assert opened.headers["x-accel-buffering"] == "no"
    assert list(opened.headers)[:3] == [
        "x-powered-by",
        "access-control-allow-origin",
        "access-control-expose-headers",
    ]


async def test_a_notification_is_accepted_with_202(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    sent = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    accepted = await http.post(f"{URL}/mcp", json=sent, headers=headers)
    assert accepted.status_code == 202
    _fronted(accepted)


async def test_the_route_ignores_case_and_a_trailing_slash(http: httpx2.AsyncClient) -> None:
    opened = await http.post(f"{URL}/MCP/", json=_init(), headers=POSTING)
    assert opened.status_code == 200
    headers = {**POSTING, "mcp-session-id": opened.headers["mcp-session-id"]}
    pinged = await http.post(f"{URL}/Mcp?x=1", json=_ping(), headers=headers)
    assert _messages(pinged) == [{"result": {}, "jsonrpc": "2.0", "id": 2}]


async def test_the_admin_api_is_left_alone(http: httpx2.AsyncClient) -> None:
    health = await http.get(f"{URL}/__fauxcall/health")
    assert health.status_code == 200
    assert "x-powered-by" not in health.headers


# -- CORS preflight and Express's 404 ------------------------------------------------------------


async def test_a_preflight_is_answered_by_cors(http: httpx2.AsyncClient) -> None:
    asked = {
        "origin": "http://localhost:6274",
        "access-control-request-method": "POST",
        "access-control-request-headers": "content-type,mcp-session-id",
    }
    answered = await http.options(f"{URL}/mcp", headers=asked)
    assert answered.status_code == 204
    assert answered.content == b""
    assert list(answered.headers.items())[:6] == [
        ("x-powered-by", "Express"),
        ("access-control-allow-origin", "*"),
        ("access-control-allow-methods", "GET,POST,DELETE"),
        ("vary", "Access-Control-Request-Headers"),
        ("access-control-allow-headers", "content-type,mcp-session-id"),
        ("access-control-expose-headers", EXPOSED),
    ]
    anywhere = await http.options(f"{URL}/anything")
    assert anywhere.status_code == 204
    assert "access-control-allow-headers" not in anywhere.headers


@pytest.mark.parametrize(
    ("method", "path", "shown"),
    [
        ("GET", "/nope", "Cannot GET /nope"),
        ("PUT", "/mcp", "Cannot PUT /mcp"),
        ("PATCH", "/MCP", "Cannot PATCH /MCP"),
        ("GET", "/mcp/extra", "Cannot GET /mcp/extra"),
        ("POST", "/mcp//", "Cannot POST /mcp//"),
        ("GET", "/%3Cb%3E", "Cannot GET /%3Cb%3E"),
        ("GET", "/openapi.json", "Cannot GET /openapi.json"),
    ],
)
async def test_unrouted_requests_get_the_express_404_page(
    http: httpx2.AsyncClient, method: str, path: str, shown: str
) -> None:
    response = await http.request(method, URL + path)
    assert response.status_code == 404
    _fronted(response)
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.text == (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>Error</title>\n'
        f"</head>\n<body>\n<pre>{shown}</pre>\n</body>\n</html>\n"
    )


async def test_a_head_gets_the_headers_only(http: httpx2.AsyncClient) -> None:
    missing = await http.head(f"{URL}/nope")
    assert (missing.status_code, missing.content) == (404, b"")
    assert missing.headers["content-length"] == str(len(express.not_found("HEAD", "/nope")[1]))
    no_session = await http.head(f"{URL}/mcp")
    assert (no_session.status_code, no_session.content) == (400, b"")
    assert no_session.headers["etag"] == NO_SESSION_ETAG
    headers = await _session(http)
    known = await http.head(f"{URL}/mcp", headers=headers)
    assert (known.status_code, known.content) == (405, b"")  # the transport: GET, POST, DELETE
    assert known.headers["allow"] == "GET, POST, DELETE"


def test_urls_are_encoded_and_pages_escaped_like_express() -> None:
    assert express.encode_url("/a b/%zz/%41/é/<x>") == "/a%20b/%25zz/%41/%C3%A9/%3Cx%3E"
    assert express.encode_url("/[x]|^\\") == "/[x]|^\\"  # left alone by encodeurl
    _, page = express.not_found("GET", "/it's\"&")
    assert b"<pre>Cannot GET /it&#39;s%22&amp;</pre>" in page
    _, spaced = express.not_found("GET  X", "/")
    assert b"<pre>Cannot GET &nbsp;X /</pre>" in spaced


# -- the route's refusals (Express) --------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("session", [None, "", "b7d6a1c2-not-issued"])
async def test_a_stream_or_delete_needs_a_known_session(
    http: httpx2.AsyncClient, method: str, session: str | None
) -> None:
    headers = {"accept": "text/event-stream"}
    if session is not None:
        headers["mcp-session-id"] = session
    refused = await http.request(method, f"{URL}/mcp", headers=headers)
    assert refused.status_code == 400  # 000018, 000019; without a session id: the source
    _fronted(refused)
    assert refused.headers["content-type"] == "application/json; charset=utf-8"
    assert refused.headers["etag"] == NO_SESSION_ETAG
    assert refused.json() == express.NO_SESSION


async def test_a_post_for_an_unknown_session_is_refused_first(http: httpx2.AsyncClient) -> None:
    headers = {"accept": "text/plain", "mcp-session-id": "nobody", "content-type": "text/plain"}
    refused = await http.post(f"{URL}/mcp", content=b"{", headers=headers)
    assert refused.status_code == 400  # before the transport's Accept or Content-Type checks
    assert refused.json() == express.NO_SESSION


async def test_a_deleted_session_is_gone(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    assert (await http.delete(f"{URL}/mcp", headers=headers)).status_code == 200
    again = await http.post(f"{URL}/mcp", json=_ping(), headers=headers)
    assert (again.status_code, again.json()) == (400, express.NO_SESSION)


# -- the transport's refusals --------------------------------------------------------------------


async def test_a_stream_must_accept_event_streams(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    refused = await http.get(f"{URL}/mcp", headers={**headers, "accept": "application/json"})
    assert refused.status_code == 406
    assert refused.json() == _sdk_error(-32000, front.NOT_ACCEPTABLE_GET)


@pytest.mark.parametrize("method", ["GET", "DELETE", "POST"])
async def test_an_unsupported_protocol_header_is_named(
    http: httpx2.AsyncClient, method: str
) -> None:
    headers = await _session(http)
    headers.update({"mcp-protocol-version": "2030-01-01", "accept": POSTING["accept"]})
    refused = await http.request(method, f"{URL}/mcp", headers=headers, json=_ping())
    assert refused.status_code == 400
    supported = "2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05, 2024-10-07"
    assert refused.json() == _sdk_error(
        -32000,
        f"Bad Request: Unsupported protocol version: 2030-01-01 (supported versions: {supported})",
    )


async def test_every_supported_protocol_header_passes(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    for version in front.SUPPORTED_VERSIONS:
        pinged = await http.post(
            f"{URL}/mcp", json=_ping(), headers={**headers, "mcp-protocol-version": version}
        )
        assert pinged.status_code == 200, version


@pytest.mark.parametrize(
    ("accept", "content_type", "status"),
    [
        ("application/json", "application/json", 406),  # 000014
        ("text/event-stream", "application/json", 406),
        (POSTING["accept"], "text/plain", 415),
        (POSTING["accept"], "application/json-patch+json", 415),
        (POSTING["accept"], "application/json, text/plain", 415),
        (POSTING["accept"], "Application/JSON; charset=utf-8", 200),
        (POSTING["accept"], 'application/json; a="x,y"', 200),
        (POSTING["accept"], "application/json;", 200),
    ],
)
async def test_accept_and_content_type(
    http: httpx2.AsyncClient, accept: str, content_type: str, status: int
) -> None:
    headers = await _session(http)
    headers.update({"accept": accept, "content-type": content_type})
    answered = await http.post(f"{URL}/mcp", content=json.dumps(_ping()), headers=headers)
    assert answered.status_code == status


@pytest.mark.parametrize(
    ("body", "refusal"),
    [
        (b"{", _sdk_error(-32700, "Parse error: Invalid JSON")),
        (b"", _sdk_error(-32700, "Parse error: Invalid JSON")),
        (b'{"a": NaN}', _sdk_error(-32700, "Parse error: Invalid JSON")),
        (b'{"hello": "world"}', _sdk_error(-32700, "Parse error: Invalid JSON-RPC message")),
        (
            json.dumps([_ping()] * 101).encode(),
            _sdk_error(-32600, "Invalid Request: Batch must not exceed 100 messages"),
        ),
        (
            json.dumps(_ping(_meta={"progressToken": 1.5})).encode(),  # 000448
            _sdk_error(-32700, "Parse error: Invalid JSON-RPC message"),
        ),
        (
            json.dumps(_ping(_meta={"progressToken": True})).encode(),  # 000449
            _sdk_error(-32700, "Parse error: Invalid JSON-RPC message"),
        ),
        (
            json.dumps(_init()).encode(),  # 000013
            _sdk_error(-32600, "Invalid Request: Server already initialized"),
        ),
    ],
)
async def test_messages_the_transport_refuses(
    http: httpx2.AsyncClient, body: bytes, refusal: JsonObject
) -> None:
    headers = await _session(http)
    refused = await http.post(f"{URL}/mcp", content=body, headers=headers)
    assert refused.status_code == 400
    _fronted(refused)
    assert refused.headers["content-type"] == "application/json"
    assert refused.json() == refusal


async def test_a_batch_of_up_to_100_is_answered(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    answered = await http.post(f"{URL}/mcp", json=[_ping()] * 100, headers=headers)
    assert answered.status_code == 200


async def test_a_body_over_4_mib_is_refused(http: httpx2.AsyncClient) -> None:
    headers = await _session(http)
    too_large = _sdk_error(-32000, front.TOO_LARGE)
    declared = await http.post(
        f"{URL}/mcp", content=b"{}", headers={**headers, "content-length": str(4 * 2**20 + 1)}
    )
    assert (declared.status_code, declared.json()) == (413, too_large)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b" " * 2**20

    streamed = await http.post(f"{URL}/mcp", content=chunks(), headers=headers)
    assert (streamed.status_code, streamed.json()) == (413, too_large)


async def test_requests_before_a_handshake(http: httpx2.AsyncClient) -> None:
    not_initialized = _sdk_error(-32000, "Bad Request: Server not initialized")
    pinged = await http.post(f"{URL}/mcp", json=_ping(), headers=POSTING)
    assert (pinged.status_code, pinged.json()) == (400, not_initialized)  # 000006
    without_client: JsonObject = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
    }
    for bad in (without_client, _init(protocolVersion=20250618)):
        refused = await http.post(f"{URL}/mcp", json=bad, headers=POSTING)
        assert (refused.status_code, refused.json()) == (400, not_initialized)  # 000511, 000512
    twice = await http.post(f"{URL}/mcp", json=[_init(), _ping()], headers=POSTING)
    assert twice.status_code == 400
    assert twice.json() == _sdk_error(
        -32600, "Invalid Request: Only one initialization request is allowed"
    )


# -- the handshake: negotiation (000508-000510) ---------------------------------------------------


@pytest.mark.parametrize(
    ("asked", "answered"),
    [
        ("2024-10-07", "2024-10-07"),  # 000508
        ("2025-11-25", "2025-11-25"),
        ("2099-01-01", "2025-11-25"),  # 000509: the latest
        ("1999-01-01", "2025-11-25"),  # 000510
    ],
)
async def test_protocol_versions(http: httpx2.AsyncClient, asked: str, answered: str) -> None:
    headers = {**POSTING, "mcp-protocol-version": "1999-01-01"}  # not checked on a handshake
    opened = await http.post(f"{URL}/mcp", json=_init(asked), headers=headers)
    assert opened.status_code == 200
    results = [m["result"] for m in _messages(opened) if isinstance(m, dict) and "result" in m]
    assert len(results) == 1
    assert isinstance(results[0], dict)
    assert results[0]["protocolVersion"] == answered


# -- what the server pushes, in the SDK's member order ---------------------------------------------


async def test_pushed_messages_put_the_method_first(app: FastAPI) -> None:
    async with connect(app, listen=True) as wire:
        await wire.call("tools/call", {"name": "gzip-file-as-resource", "arguments": {}})
        await asyncio.sleep(0.05)  # the push reaches the stream after the answer
        stream = await wire.stop_listening()
    assert stream is not None
    pushed = [data for _, _, data, _ in stream.sse_events if data]
    assert pushed == ['{"method":"notifications/resources/list_changed","jsonrpc":"2.0"}']


def test_events_are_rewritten_as_the_sdk_writes_them() -> None:
    """``event`` before ``id``, members in the SDK's order, lengths kept, split chunks joined."""
    events = sse.SseRewriter(prime=False)
    first = events.feed(
        b'id: 1\nevent: message\ndata: {"jsonrpc":"2.0","method":"x","pa', last=False
    )
    rest = events.feed(
        b'rams":{"a":1.50}}\n\nid: 2\nevent: message\ndata: {"result":{},"jsonrpc":"2.0","id":1}\n\n'
        b": keepalive\n\n",
        last=True,
    )
    assert first == b""
    assert rest == (
        b'event: message\nid: 1\ndata: {"method":"x","params":{"a":1.50},"jsonrpc":"2.0"}\n\n'
        b'event: message\nid: 2\ndata: {"result":{},"jsonrpc":"2.0","id":1}\n\n'
        b": keepalive\n\n"
    )
    request = b'data: {"jsonrpc":"2.0","id":7,"method":"roots/list"}\n\ndata: not json\n\n'
    assert sse.SseRewriter(prime=False).feed(request, last=True) == (
        b'data: {"method":"roots/list","jsonrpc":"2.0","id":7}\n\ndata: not json\n\n'
    )


@pytest.mark.parametrize(
    ("prime", "body", "primed"),
    [
        (True, b"id: p\ndata: \n\nevent: message\nid: 1\ndata: {}\n\n", True),  # kept
        (False, b"id: p\ndata: \n\nevent: message\nid: 1\ndata: {}\n\n", False),  # dropped
        (True, b"event: message\nid: 1\ndata: {}\n\n", True),  # added
        (True, b": keepalive\n\n", True),  # before a long call's first keep-alive
        (False, b"event: message\nid: 1\ndata: {}\n\n", False),
    ],
)
def test_priming_follows_the_asked_version(*, prime: bool, body: bytes, primed: bool) -> None:
    written = sse.SseRewriter(prime=prime).feed(body, last=True)
    first = written.split(b"\n\n", 1)[0].split(b"\n")
    assert (first[0].startswith(b"id: ") and first[1:] == [b"data: "]) is primed
    assert written.count(b"data: \n") == int(primed)


# -- the schemas -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "valid"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, True),
        ({"jsonrpc": "2.0", "id": "a", "method": "ping", "params": {}}, True),
        ({"jsonrpc": "2.0", "id": 1.0, "method": "ping"}, True),
        ({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, False),
        ({"jsonrpc": "2.0", "id": 2**53, "method": "ping"}, False),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, False),
        ({"jsonrpc": "2.0", "id": None, "method": "ping"}, False),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "extra": 1}, False),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None}, False),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, False),
        ({"jsonrpc": "2.0", "id": 1, "method": 5}, False),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, False),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, True),
        ({"jsonrpc": "2.0", "method": "n", "params": {"_meta": {"progressToken": "t"}}}, True),
        ({"jsonrpc": "2.0", "method": "n", "params": {"_meta": None}}, False),
        ({"jsonrpc": "2.0", "id": 1, "result": {}}, True),
        ({"jsonrpc": "2.0", "id": 1, "result": {"_meta": {"progressToken": 1.5}}}, False),
        ({"jsonrpc": "2.0", "id": 1, "result": []}, False),
        ({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "m", "data": [1]}}, True),
        ({"jsonrpc": "2.0", "error": {"code": -1, "message": "m", "more": 1}}, True),
        ({"jsonrpc": "2.0", "error": {"code": 1.5, "message": "m"}}, False),
        ({"jsonrpc": "2.0", "id": 1, "error": {"code": 1}, "result": {}}, False),
        ({"jsonrpc": "2.0", "id": 1}, False),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"_meta": {jsonrpc.RELATED_TASK: {"taskId": 5}}},
            },
            False,
        ),
        ([], False),
    ],
)
def test_json_rpc_messages(message: JsonValue, *, valid: bool) -> None:
    assert jsonrpc.is_message(message) is valid


@pytest.mark.parametrize(
    ("changes", "valid"),
    [
        ({}, True),
        ({"capabilities": {"elicitation": {}}}, True),
        ({"capabilities": {"elicitation": {"form": {"applyDefaults": "yes"}}}}, False),
        ({"capabilities": {"elicitation": {"url": []}}}, True),  # arrays are objects to typeof
        ({"capabilities": {"elicitation": None}}, False),
        ({"capabilities": {"roots": {"listChanged": 1}}}, False),
        ({"capabilities": {"sampling": {"tools": {}}}}, True),
        ({"capabilities": {"tasks": {"requests": {"sampling": {"createMessage": 1}}}}}, False),
        ({"capabilities": {"experimental": {"x": None}}}, False),
        ({"capabilities": {"anything": 1}}, True),
        ({"capabilities": []}, False),
        ({"clientInfo": {"name": "p"}}, False),
        ({"clientInfo": _client(icons=[{"src": "i.png", "theme": "dark"}])}, True),
        ({"clientInfo": _client(icons=[{"src": "i.png", "theme": "blue"}])}, False),
        ({"clientInfo": _client(icons=[{"src": "i.png", "sizes": [48]}])}, False),
        ({"clientInfo": _client(websiteUrl=1)}, False),
        ({"protocolVersion": None}, False),
    ],
)
def test_handshakes(changes: JsonObject, *, valid: bool) -> None:
    assert jsonrpc.is_initialize_request(_init(**changes)) is valid


@pytest.mark.parametrize(
    ("header", "essence"),
    [
        (None, None),
        ("", None),
        ("application/json", "application/json"),
        (" Application/Json ; charset=utf-8", "application/json"),
        ("application/json;", "application/json"),
        ("application/json; charset", "application/json"),
        ("application/json, text/plain", "application/json, text/plain"),
        ("application/json;a=b,c", None),  # unparsable, and a comma after the type
        ("text/plain; x=application/json", "text/plain"),
        ("not a type", "not a type"),
    ],
)
def test_media_types(header: str | None, essence: str | None) -> None:
    assert front.media_type_essence(header) == essence
