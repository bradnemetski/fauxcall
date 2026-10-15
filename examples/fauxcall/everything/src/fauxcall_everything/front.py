"""The real server's HTTP front: Express 5, its ``cors`` middleware and the SDK transport's checks.

The real server (``server-everything``'s ``transports/streamableHttp.js``) is an Express app:
``cors`` answers preflights and marks every answer, ``POST``, ``GET`` and ``DELETE /mcp`` (matched
in any case, with one optional trailing slash; ``HEAD`` takes the ``GET`` route) refuse a session
they don't have, and anything else gets Express's 404 page (see :mod:`fauxcall_everything.express`).
The rest goes to the SDK's Streamable HTTP transport, which checks in order: ``Accept`` (000014),
``Content-Type``, the body's size (4 MiB), that it is JSON, a batch's length (100), that every
message is JSON-RPC (000448), the handshake rules (000013, 000511) and the
``MCP-Protocol-Version`` header. The SDK then looks up a request's related task before its
handler runs (000520-000525), and parses an ``initialize`` inside a session like any request
(000514-000517).

FauxCall's runtime answers MCP itself. This middleware stands in front of it: it answers what the
real server refuses before MCP is reached, in the real order, with the real bodies and headers,
and journals those answers like the runtime does; everything else passes through, its answers
dressed as the real server writes them (headers, and SSE events as in
:mod:`fauxcall_everything.sse`). A DELETE that ends a session also ends that session's GET
stream, as the transport's ``close()`` does. The admin API (``/__fauxcall``) passes untouched.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Final

from fauxcall.clock import ns_to_datetime
from fauxcall.runtime.journal import JournalEntry, body_for_journal
from fauxcall.runtime.mcp_server import method_endpoint, tool_endpoint
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect, HTTPConnection

from fauxcall_everything.express import (
    FRONT,
    NO_SESSION,
    express_json,
    not_found,
    preflight_headers,
)
from fauxcall_everything.jsonrpc import (
    RELATED_TASK,
    initialize_issues,
    is_initialize_request,
    is_message,
)
from fauxcall_everything.jsvalues import JS_WHITESPACE, parse_number
from fauxcall_everything.sse import DEFAULT_NEGOTIATED, SseRewriter, message_event, primes
from fauxcall_everything.state import tasks_of
from fauxcall_everything.surrogates import hide
from fauxcall_everything.zodish import issues_text

if TYPE_CHECKING:
    from fauxcall.runtime import FakeRuntime
    from fauxcall.types import JsonObject, JsonValue
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    RawHeaders = list[tuple[bytes, bytes]]
    Answer = tuple[int, RawHeaders, bytes]

MCP_ROUTE: Final = re.compile(r"/mcp/?", re.IGNORECASE)
ROUTE_METHODS: Final = frozenset({"GET", "HEAD", "POST", "DELETE"})
SUPPORTED_VERSIONS: Final = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
"""The SDK's ``SUPPORTED_PROTOCOL_VERSIONS``, in its order (the recorded refusal lists them)."""
SERVER_METHODS: Final = frozenset(
    {
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "resources/subscribe",
        "resources/unsubscribe",
        "prompts/list",
        "prompts/get",
        "completion/complete",
        "logging/setLevel",
        "tasks/get",
        "tasks/result",
        "tasks/list",
        "tasks/cancel",
    }
)
"""The methods the real server has handlers for (any other is "Method not found": 000524)."""
MAX_BODY: Final = 4 * 1024 * 1024
MAX_BATCH: Final = 100
SSE: Final = b"text/event-stream"
NOT_ACCEPTABLE_POST: Final = (
    "Not Acceptable: Client must accept both application/json and text/event-stream"
)
NOT_ACCEPTABLE_GET: Final = "Not Acceptable: Client must accept text/event-stream"
UNSUPPORTED_MEDIA_TYPE: Final = "Unsupported Media Type: Content-Type must be application/json"
TOO_LARGE: Final = f"Payload Too Large: Request body must not exceed {MAX_BODY} bytes"

_TCHAR: Final = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_MEDIA_TYPE: Final = re.compile(rf"{_TCHAR}/{_TCHAR}")
_QUOTED: Final = r'"(?:[\x0b\x20\x21\x23-\x5b\x5d-\x7e\x80-\xff]|\\[\x0b\x20-\xff])*"'
_PARAMETER: Final = re.compile(rf"; *{_TCHAR} *= *(?:{_QUOTED}|{_TCHAR}) *")
"""The ``content-type`` package's ``PARAM_REGEXP``."""


class ExpressFront:
    """ASGI middleware: the real server's Express front, before FauxCall's MCP runtime.

    Args:
        app: The fake's app (FauxCall's runtime).
        admin_prefix: Where FauxCall's admin API lives (passed through untouched).
        runtime: The runtime, whose journal records this front's own answers too.
    """

    def __init__(self, app: ASGIApp, admin_prefix: str, runtime: FakeRuntime) -> None:
        self.app = app
        self.admin_prefix = admin_prefix.rstrip("/")
        self.runtime = runtime
        self.sessions: set[str] = set()
        """MCP sessions the fake issued and no DELETE ended (the server's ``transports`` map)."""
        self.streams: dict[str, set[OpenStream]] = {}
        """The GET streams open per MCP session (a DELETE ends them)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: PLR0911 - one answer per refusal
        """Answer as the real server's front does, or pass the request on to the runtime."""
        if scope["type"] != "http" or self._is_admin(scope["path"]):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        method = str(scope["method"])
        headers = Headers(scope=scope)
        path = raw_path(scope)
        head = method == "HEAD"
        if method == "OPTIONS":
            await self._answer(scope, send, (204, preflight_headers(headers), b""), started)
            return
        if not MCP_ROUTE.fullmatch(path) or method not in ROUTE_METHODS:
            await self._answer(scope, send, (404, *not_found(method, path)), started, head=head)
            return
        if path != "/mcp":  # /MCP, /mcp/: the same route for Express; the runtime serves /mcp
            scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
        session = headers.get("mcp-session-id") or ""
        if (session or method != "POST") and session not in self.sessions:
            await self._answer(scope, send, (400, *express_json(NO_SESSION)), started, head=head)
            return
        if method == "POST":
            await self._post(scope, receive, send, session=session, started=started)
            return
        refusal = _get_or_delete_refusal(method, headers)
        if refusal is not None:
            await self._answer(scope, send, refusal, started, head=head)
            return
        if method == "GET":
            await self._stream(scope, receive, send, session=session)
            return
        pushes = self.runtime.mcp.streams.get(session) if self.runtime.mcp is not None else None
        dressed = self._dressed(send, method, session, prime=False, pushes=pushes)
        await self.app(scope, receive, dressed)

    def _is_admin(self, path: str) -> bool:
        return path == self.admin_prefix or path.startswith(self.admin_prefix + "/")

    async def _post(
        self, scope: Scope, receive: Receive, send: Send, *, session: str, started: float
    ) -> None:
        """The transport's checks of a POST, in its order; a POST that passes goes through."""
        headers = Headers(scope=scope)
        accept = headers.get("accept") or ""
        if "application/json" not in accept or "text/event-stream" not in accept:
            await self._answer(scope, send, sdk_error(406, -32000, NOT_ACCEPTABLE_POST), started)
            return
        if not is_json_content_type(headers.get("content-type")):
            refused = sdk_error(415, -32000, UNSUPPORTED_MEDIA_TYPE)
            await self._answer(scope, send, refused, started)
            return
        body = await read_body(headers, receive)
        if body is None:
            await self._answer(scope, send, sdk_error(413, -32000, TOO_LARGE), started)
            return
        text = hide(decoded(body))  # lone surrogates: see surrogates.py
        try:
            parsed = parse_json(text)
        except (ValueError, RecursionError):
            refused = sdk_error(400, -32700, "Parse error: Invalid JSON")
            await self._answer(scope, send, refused, started, sent=body)
            return
        messages: list[JsonValue] = [*parsed] if isinstance(parsed, list) else [parsed]
        refusal = _message_refusal(parsed, messages, headers, session=session)
        if refusal is not None:
            await self._answer(scope, send, refusal, started, sent=body)
            return
        if session and len(messages) == 1:
            reply = self._rpc_refusal(scope, messages[0], session)
            if reply is not None:
                await self._rpc_answer(
                    scope,
                    send,
                    headers=headers,
                    session=session,
                    message=messages[0],
                    reply=reply,
                    started=started,
                )
                return
        prime = _primes(messages, headers)
        scope = _with_json_type(_without_unsupported_version(scope) if not session else scope)
        dressed = self._dressed(send, "POST", session, prime=prime)
        await self.app(scope, _replayed(text.encode(), receive), dressed)

    def _rpc_refusal(self, scope: Scope, message: JsonValue, session: str) -> JsonObject | None:
        """What the SDK answers a request before its handler would, if anything.

        A related task the client doesn't have is "Task not found" for every method the server
        handles; an ``initialize`` inside a session is parsed as a request, and refused by zod.
        """
        if not isinstance(message, dict) or "id" not in message or "method" not in message:
            return None
        method = str(message["method"])
        params = message.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        related = meta.get(RELATED_TASK) if isinstance(meta, dict) else None
        task_id = related.get("taskId") if isinstance(related, dict) else None
        if method in SERVER_METHODS and task_id and not self._has_task(scope, session, task_id):
            return _error(message, -32602, f"MCP error -32602: Task not found: {task_id}")
        if method == "initialize" and not is_initialize_request(message):
            return _error(message, -32603, issues_text(initialize_issues(message)))
        return None

    def _has_task(self, scope: Scope, session: str, task_id: object) -> bool:
        evaluation = self.runtime.session_of(HTTPConnection(scope))
        task = tasks_of(self.runtime, evaluation, session).get(str(task_id))
        now = self.runtime.clocks.for_session(evaluation).now_ns()
        return task is not None and not task.gone(task.time(now))

    async def _rpc_answer(  # noqa: PLR0913 - the request, the session and the reply
        self,
        scope: Scope,
        send: Send,
        *,
        headers: Headers,
        session: str,
        message: JsonValue,
        reply: JsonObject,
        started: float,
    ) -> None:
        """A JSON-RPC answer on the POST's SSE stream, as the transport writes it."""
        asked = headers.get("mcp-protocol-version")
        opening = SseRewriter(prime=primes(DEFAULT_NEGOTIATED if asked is None else asked))
        body = opening.feed(message_event(reply), last=True)
        answer_headers = dressed_headers(
            [(b"content-type", SSE), (b"content-length", str(len(body)).encode())], session
        )
        await send({"type": "http.response.start", "status": 200, "headers": answer_headers})
        await send({"type": "http.response.body", "body": body})
        self._journal(scope, 200, answer_headers, b"", started, message=message, reply=reply)

    async def _stream(self, scope: Scope, receive: Receive, send: Send, *, session: str) -> None:
        """A GET stream, passed through until it ends or a DELETE ends its session."""
        stream = OpenStream(send)
        self.streams.setdefault(session, set()).add(stream)
        try:
            dressed = self._dressed(stream.forward, "GET", session, prime=False)
            await self.app(scope, receive, dressed)
        except (OSError, ClientDisconnect):
            if not stream.ended.is_set():
                raise
        finally:
            self.streams.get(session, set()).discard(stream)

    async def _end_streams(self, session: str, pushes: asyncio.Queue[JsonValue] | None) -> None:
        """Finish the session's GET streams, and wake the runtime's loop writing to them.

        Its next write then fails, as to a client that went away, and the loop ends there.
        """
        for stream in self.streams.pop(session, set()):
            await stream.end()
        if pushes is not None:
            pushes.put_nowait(None)

    def _dressed(
        self,
        send: Send,
        method: str,
        session: str,
        *,
        prime: bool,
        pushes: asyncio.Queue[JsonValue] | None = None,
    ) -> Send:
        """``send``, dressing the answer as the real server writes it and following sessions."""
        events: list[SseRewriter] = []
        held: list[Message] = []  # an SSE answer's start, until its length is known

        async def dressed(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = int(message["status"])
                raw: RawHeaders = list(message.get("headers", []))
                issued = dict(raw).get(b"mcp-session-id")
                if method == "POST" and issued and status < 400:  # noqa: PLR2004
                    self.sessions.add(issued.decode("latin-1"))
                if method == "DELETE" and status == 200:  # noqa: PLR2004
                    self.sessions.discard(session)
                    await self._end_streams(session, pushes)
                message = {**message, "headers": dressed_headers(raw, session)}
                if dict(raw).get(b"content-type", b"").startswith(SSE):
                    events.append(SseRewriter(prime=prime))
                    if b"content-length" in dict(raw):  # a whole answer: its length will change
                        held.append(message)
                        return
            elif message["type"] == "http.response.body" and events:
                last = not message.get("more_body", False)
                body = events[0].feed(bytes(message.get("body", b"")), last=last)
                if held:
                    await send(_with_length(held.pop(), len(body) if last else None))
                message = {**message, "body": body}
            await send(message)

        return dressed

    async def _answer(  # noqa: PLR0913 - an answer, and what the journal notes of it
        self,
        scope: Scope,
        send: Send,
        answer: Answer,
        started: float,
        *,
        head: bool = False,
        sent: bytes = b"",
    ) -> None:
        """Answer the request here (a ``HEAD`` gets the headers only), and journal it."""
        status, headers, body = answer
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if head else body})
        self._journal(scope, status, headers, sent, started, answered=b"" if head else body)

    def _journal(  # noqa: PLR0913 - every member of a journal entry
        self,
        scope: Scope,
        status: int,
        headers: RawHeaders,
        sent: bytes,
        started: float,
        *,
        answered: bytes = b"",
        message: JsonValue = None,
        reply: JsonObject | None = None,
    ) -> None:
        """Note an answer of this front in the runtime's journal, as the runtime notes its own."""
        if not self.runtime.journal.enabled:
            return
        connection = HTTPConnection(scope)
        ctx = self.runtime.new_context(connection, None)
        answer_headers = Headers(raw=headers)
        request_body: JsonValue
        response_body: JsonValue
        request_text: str | None
        response_text: str | None
        if isinstance(message, dict):
            method = str(message.get("method"))
            params = message.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            endpoint: str | None = (
                tool_endpoint(str(name)) if method == "tools/call" else method_endpoint(method)
            )
            request_body, request_text = message, None
            response_body, response_text = reply, None
        else:
            endpoint, method = None, None
            sent_type = connection.headers.get("content-type")
            request_body, request_text = body_for_journal(sent, sent_type)
            answered_type = answer_headers.get("content-type")
            response_body, response_text = body_for_journal(answered, answered_type)
        self.runtime.journal.add(
            JournalEntry(
                seq=ctx.seq,
                session=ctx.session,
                request_id=ctx.request_id,
                at=ns_to_datetime(ctx.now_ns),
                method=str(scope["method"]),
                path=raw_path(scope),
                query=self.runtime.redact(connection.query_params.multi_items()),
                endpoint=endpoint,
                request_headers=self.runtime.redact(connection.headers.items()),
                request_body=request_body,
                request_text=request_text,
                status=status,
                response_headers=self.runtime.redact(answer_headers.items()),
                response_body=response_body,
                response_text=response_text,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                mcp_method=method,
            )
        )


class OpenStream:
    """A GET stream on its way to the client, which a DELETE of its session can end.

    Ending it finishes the response at once (the client sees the stream close); the runtime's
    next write to it fails, as to a client that went away.
    """

    def __init__(self, send: Send) -> None:
        self.send = send
        self.lock = asyncio.Lock()
        self.started = False
        self.done = False
        self.ended = asyncio.Event()

    async def forward(self, message: Message) -> None:
        """Send on, unless the stream was ended."""
        async with self.lock:
            if self.done:
                raise OSError("the session ended")
            if message["type"] == "http.response.start":
                self.started = True
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                self.done = True
            await self.send(message)

    async def end(self) -> None:
        """Finish the response now."""
        self.ended.set()
        async with self.lock:
            if self.started and not self.done:
                self.done = True
                await self.send({"type": "http.response.body", "body": b"", "more_body": False})


def _message_refusal(  # noqa: PLR0911 - one answer per refusal, in the transport's order
    parsed: JsonValue, messages: list[JsonValue], headers: Headers, *, session: str
) -> Answer | None:
    """The transport's refusal of a POST's messages, in its order."""
    if isinstance(parsed, list) and len(parsed) > MAX_BATCH:
        refused = f"Invalid Request: Batch must not exceed {MAX_BATCH} messages"
        return sdk_error(400, -32600, refused)
    if not all(is_message(message) for message in messages):
        return sdk_error(400, -32700, "Parse error: Invalid JSON-RPC message")
    if any(is_initialize_request(message) for message in messages):
        if session:
            return sdk_error(400, -32600, "Invalid Request: Server already initialized")
        if len(messages) > 1:
            refused = "Invalid Request: Only one initialization request is allowed"
            return sdk_error(400, -32600, refused)
        return None
    if not session:  # a fresh transport, never initialized (000006, 000511)
        return sdk_error(400, -32000, "Bad Request: Server not initialized")
    return version_refusal(headers)


def _primes(messages: list[JsonValue], headers: Headers) -> bool:
    """Whether the POST's answer opens with a priming event (``sse.py``)."""
    for message in messages:
        if is_initialize_request(message):
            assert isinstance(message, dict)  # noqa: S101 - a handshake is an object
            params = message["params"]
            assert isinstance(params, dict)  # noqa: S101
            return primes(str(params["protocolVersion"]))
    asked = headers.get("mcp-protocol-version")
    return primes(DEFAULT_NEGOTIATED if asked is None else asked)


def _error(message: JsonObject, code: int, text: str) -> JsonObject:
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": code, "message": text}}


def _get_or_delete_refusal(method: str, headers: Headers) -> Answer | None:
    """The transport's refusal of a GET (or HEAD) or DELETE for a session it has."""
    if method == "HEAD":  # the transport handles GET, POST and DELETE only
        refused: JsonObject = {"code": -32000, "message": "Method not allowed."}
        allow = [(b"allow", b"GET, POST, DELETE")]
        return _json(405, {"jsonrpc": "2.0", "error": refused, "id": None}, extra=allow)
    if method == "GET" and SSE.decode() not in (headers.get("accept") or ""):
        return sdk_error(406, -32000, NOT_ACCEPTABLE_GET)
    return version_refusal(headers)


def version_refusal(headers: Headers) -> Answer | None:
    """``validateProtocolVersion``: an ``MCP-Protocol-Version`` the SDK doesn't support."""
    version = headers.get("mcp-protocol-version")
    if version is None or version in SUPPORTED_VERSIONS:
        return None
    supported = ", ".join(SUPPORTED_VERSIONS)
    refused = (
        f"Bad Request: Unsupported protocol version: {version} (supported versions: {supported})"
    )
    return sdk_error(400, -32000, refused)


def _without_unsupported_version(scope: Scope) -> Scope:
    """A handshake's ``MCP-Protocol-Version`` isn't checked: the runtime shouldn't check it."""
    version = Headers(scope=scope).get("mcp-protocol-version")
    if version is None or version in SUPPORTED_VERSIONS:
        return scope
    kept = [(k, v) for k, v in scope["headers"] if k.lower() != b"mcp-protocol-version"]
    return {**scope, "headers": kept}


def _with_json_type(scope: Scope) -> Scope:
    """The scope with ``Content-Type: application/json``, the type the transport parsed.

    ``Application/JSON; charset=utf-8`` is JSON to the transport; the runtime looks for the text.
    """
    kept = [(k, v) for k, v in scope["headers"] if k.lower() != b"content-type"]
    return {**scope, "headers": [*kept, (b"content-type", b"application/json")]}


def _with_length(start: Message, length: int | None) -> Message:
    """An answer's start with the length of its rewritten body (``None``: streamed, no length)."""
    kept = [(k, v) for k, v in start["headers"] if k != b"content-length"]
    if length is not None:
        kept.append((b"content-length", str(length).encode()))
    return {**start, "headers": kept}


def raw_path(scope: Scope) -> str:
    """The request's path as sent (Express matches and prints it undecoded)."""
    raw = scope.get("raw_path")
    if isinstance(raw, bytes) and raw:
        return raw.split(b"?", 1)[0].decode("latin-1")
    return str(scope["path"])


def is_json_content_type(header: str | None) -> bool:
    """The SDK's ``isJsonContentType``: the parsed media type is ``application/json``."""
    if header == "application/json":
        return True
    return media_type_essence(header) == "application/json"


def media_type_essence(header: str | None) -> str | None:
    """The SDK's ``mediaTypeEssence``: ``content-type``'s parse, or its lenient fallback."""
    if not header:
        return None
    index = header.find(";")
    kind = (header[:index] if index != -1 else header).strip(JS_WHITESPACE)
    if _MEDIA_TYPE.fullmatch(kind) and _parameters_parse(header, index):
        return kind.lower()
    essence = header.split(";", 1)[0].strip(JS_WHITESPACE).lower()
    if not essence or "," in header[len(essence) :]:
        return None
    return essence


def _parameters_parse(header: str, index: int) -> bool:
    """Whether the ``content-type`` package parses the parameters from ``index`` on."""
    if index == -1:
        return True
    while (match := _PARAMETER.match(header, index)) is not None:
        index = match.end()
    return index == len(header)


async def read_body(headers: Headers, receive: Receive) -> bytes | None:
    """The request body (``None``: over 4 MiB, by its ``Content-Length`` or as it arrives)."""
    declared = headers.get("content-length")
    if declared is not None and parse_number(declared) > MAX_BODY:
        return None
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = bytes(message.get("body", b""))
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def decoded(body: bytes) -> str:
    """The body as ``TextDecoder`` reads it: a byte-order mark dropped, bad bytes replaced."""
    return body.decode("utf-8", errors="replace").removeprefix("﻿")


def parse_json(text: str) -> JsonValue:
    """``JSON.parse``.

    Raises:
        ValueError: The text is not JSON (``NaN`` and ``Infinity`` are not JSON either).
    """

    def no_constants(name: str) -> JsonValue:
        raise ValueError(name)

    parsed: JsonValue = json.loads(text, parse_constant=no_constants)
    return parsed


def _replayed(body: bytes, receive: Receive) -> Receive:
    """A ``receive`` that hands the body already read on, then the client's next messages."""
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def replay() -> Message:
        return pending.pop() if pending else await receive()

    return replay


def dressed_headers(raw: RawHeaders, session: str = "") -> RawHeaders:
    """An answer's headers as the real server writes them.

    Express's and the CORS headers come first; an SSE answer gets the transport's exact type,
    its cache headers and, in a session, the session's id (000218, 000460).
    """
    names = {name for name, _ in FRONT}
    rest = [(name.lower(), value) for name, value in raw if name.lower() not in names]
    if any(name == b"content-type" and value.startswith(SSE) for name, value in rest):
        kept = [(n, SSE if n == b"content-type" else v) for n, v in rest if n != b"cache-control"]
        if session and all(name != b"mcp-session-id" for name, _ in kept):
            kept.append((b"mcp-session-id", session.encode("latin-1")))
        rest = [
            (b"cache-control", b"no-cache, no-transform"),
            *kept,
            (b"x-accel-buffering", b"no"),
        ]
    return [*FRONT, *rest]


def sdk_error(status: int, code: int, message: str) -> Answer:
    """The transport's ``createJsonErrorResponse``."""
    return _json(
        status, {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": None}
    )


def _json(status: int, payload: JsonValue, extra: RawHeaders | None = None) -> Answer:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    headers: RawHeaders = [
        *FRONT,
        *(extra or []),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    return status, headers, body
