"""A wire-level MCP client for recording, introspection and parity tests.

Speaks Streamable HTTP (and the legacy HTTP+SSE transport) directly over ``httpx2`` and keeps every
byte: status codes, headers, SSE framing and the exact JSON of each message.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urljoin

import httpx2

from fauxcall import __version__, jsonfmt
from fauxcall.errors import HttpStatusError, RecordingError
from fauxcall.mcpwire import protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from fauxcall.types import JsonObject, JsonValue

Transport = Literal["streamable_http", "sse"]

MAX_PAGES: Final = 1000
"""Upper bound on pages followed by :meth:`McpWireClient.list_all`."""

_LEGACY_VERSION: Final = "2024-11-05"
"""The protocol revision that predates the ``MCP-Protocol-Version`` header."""

type _QueuedEvent = tuple[float, str, str, str | None] | None
"""``(perf_counter time, event, data, id)`` read from the legacy SSE stream; ``None`` = it ended."""


@dataclass(slots=True)
class RpcCall:
    """One HTTP exchange carrying JSON-RPC (everything needed to record or compare it)."""

    method: str
    sent: JsonValue
    status: int
    request_headers: list[tuple[str, str]]
    response_headers: list[tuple[str, str]]
    content_type: str | None
    body: str
    messages: list[JsonValue]
    response: JsonValue
    started_ns: int
    elapsed_ms: float
    sse_events: list[tuple[float, str, str, str | None]] = field(default_factory=list)
    """``(ms since the request started, event, data, id)`` for every SSE event received."""
    http_method: str = "POST"

    def header(self, name: str) -> str | None:
        """A response header (case-insensitive)."""
        lowered = name.lower()
        return next((v for k, v in self.response_headers if k.lower() == lowered), None)

    @property
    def result(self) -> JsonValue:
        """The ``result`` member of the response, if any."""
        return self.response.get("result") if isinstance(self.response, dict) else None

    @property
    def error(self) -> JsonValue:
        """The ``error`` member of the response, if any."""
        return self.response.get("error") if isinstance(self.response, dict) else None


def _is_event_stream(content_type: str | None) -> bool:
    return bool(content_type) and "text/event-stream" in str(content_type).lower()


def _parse_lenient(body: str, content_type: str | None, status: int) -> list[JsonValue]:
    """Messages in a body; error bodies that aren't valid JSON-RPC yield nothing."""
    messages: list[JsonValue] = []
    with contextlib.suppress(ValueError):
        if status < 300:  # noqa: PLR2004 - success range
            messages = protocol.messages_from_body(body, content_type)
        if not messages and body.strip().startswith("{"):
            messages = [jsonfmt.loads(body)]
    return messages


def _reply_to(messages: list[JsonValue], wanted: JsonValue) -> JsonValue:
    """The response to request ``wanted``; else an error the server could not address (id null)."""
    responses = [m for m in messages if protocol.is_response(m) and isinstance(m, dict)]
    exact = next((m for m in responses if m.get("id") == wanted), None)
    if exact is not None:
        return exact
    return next((m for m in responses if m.get("id") is None and "error" in m), None)


@dataclass
class _Listening:
    """The client's GET stream, held open by :meth:`McpWireClient.listen`."""

    headers: dict[str, str]
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    status: int | None = None
    content_type: str | None = None
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    body: str = ""
    events: list[tuple[float, str, str, str | None]] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)
    started_ns: int = field(default_factory=time.time_ns)


def _json_or_nothing(data: str) -> list[JsonValue]:
    """An event's JSON message, if it carries one."""
    try:
        return [jsonfmt.loads(data)]
    except ValueError:
        return []


class McpWireClient:
    """Minimal MCP client that records everything."""

    def __init__(
        self,
        url: str,
        http: httpx2.AsyncClient,
        *,
        headers: Mapping[str, str] | None = None,
        protocol_version: str = protocol.LATEST_HANDSHAKE_VERSION,
        transport: Transport = "streamable_http",
        timeout_s: float = 60.0,
    ) -> None:
        self.url = url
        self.http = http
        self.headers = dict(headers or {})
        self.protocol_version = protocol_version
        self.transport: Transport = transport
        self.timeout_s = timeout_s
        self.session_id: str | None = None
        self.negotiated_version: str | None = None
        self.on_request: Callable[[JsonObject], Awaitable[JsonValue | None]] | None = None
        """Answers requests the server sends mid-call (``sampling/createMessage`` …): the reply
        message to POST back, or ``None`` to leave one unanswered."""
        self.sse_content_type: str | None = None
        """Content type of the legacy transport's event stream (once opened)."""
        self._ids = itertools.count(1)
        self._sse_messages_url: str | None = None
        self._sse_queue: asyncio.Queue[_QueuedEvent] | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._listening: _Listening | None = None
        self._sse_stream: contextlib.AbstractAsyncContextManager[httpx2.Response] | None = None
        self._sse_ended = False

    @property
    def messages_url(self) -> str | None:
        """Legacy SSE transport: the URL the server announced for POSTing messages."""
        return self._sse_messages_url

    # -- request plumbing ------------------------------------------------------------------------

    async def _answer_server(self, data: str) -> None:
        """Answer a request the server sent mid-call (sampling …), when the client knows how."""
        if self.on_request is None:
            return
        try:
            message = jsonfmt.loads(data)
        except ValueError:
            return
        if not protocol.is_request(message) or not isinstance(message, dict):
            return
        reply = await self.on_request(message)
        if reply is not None:
            await self.post(reply)

    def _version_header(self, headers: dict[str, str]) -> dict[str, str]:
        if self.negotiated_version and self.negotiated_version != _LEGACY_VERSION:
            headers[protocol.PROTOCOL_HEADER] = self.negotiated_version
        return headers

    def _request_headers(self, *, include_session: bool) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            **self.headers,
        }
        if include_session and self.session_id:
            headers[protocol.SESSION_HEADER] = self.session_id
        return self._version_header(headers)

    async def post(
        self,
        message: JsonValue,
        *,
        include_session: bool = True,
        extra_headers: Mapping[str, str] | None = None,
    ) -> RpcCall:
        """POST one JSON-RPC message (or batch) and collect the full response.

        An SSE response is read until the reply to ``message`` arrives (or the server ends the
        stream); every event is kept with the time it arrived.
        """
        if self.transport == "sse":
            return await self._post_sse(message)
        headers = {
            **self._request_headers(include_session=include_session),
            **(extra_headers or {}),
        }
        method = str(message.get("method", "")) if isinstance(message, dict) else "batch"
        wanted = message.get("id") if isinstance(message, dict) else None
        started_ns = time.time_ns()
        started = time.perf_counter()
        events: list[tuple[float, str, str, str | None]] = []
        chunks: list[str] = []
        async with self.http.stream(
            "POST",
            self.url,
            content=jsonfmt.dumps(message).encode(),
            headers=headers,
            timeout=self.timeout_s,
        ) as response:
            content_type = response.headers.get("content-type")
            decoder = protocol.SseDecoder() if _is_event_stream(content_type) else None
            answered = False
            async for text in response.aiter_text():
                chunks.append(text)
                if decoder is None:
                    continue
                stamp = (time.perf_counter() - started) * 1000
                for event in decoder.feed(text):
                    events.append((stamp, event.event, event.data, event.id))
                    answered = answered or _answers(event, wanted)
                    await self._answer_server(event.data)
                if answered:
                    break
            if decoder is not None and not answered:
                stamp = (time.perf_counter() - started) * 1000
                events += [
                    (stamp, event.event, event.data, event.id)
                    for event in decoder.close(dispatch_incomplete=True)
                ]
        elapsed = (time.perf_counter() - started) * 1000
        body = "".join(chunks)
        messages = _parse_lenient(body, content_type, response.status_code)
        return RpcCall(
            method=method,
            sent=message,
            status=response.status_code,
            request_headers=list(headers.items()),
            response_headers=list(response.headers.multi_items()),
            content_type=content_type,
            body=body,
            messages=messages,
            response=_reply_to(messages, wanted),
            started_ns=started_ns,
            elapsed_ms=elapsed,
            sse_events=events,
        )

    # -- the protocol ------------------------------------------------------------------------------

    async def initialize(
        self,
        *,
        protocol_version: str | None = None,
        capabilities: JsonObject | None = None,
        announce: bool = True,
    ) -> RpcCall:
        """Open a session (``initialize`` + ``notifications/initialized``).

        ``capabilities`` are the client's (none by default): some servers list more tools to
        clients that support sampling, elicitation or roots. ``announce=False`` leaves out
        ``notifications/initialized`` (send it later with :meth:`notify`).
        """
        if self.transport == "sse" and self._sse_task is None:
            await self._open_sse()
        params: JsonObject = {
            "protocolVersion": protocol_version or self.protocol_version,
            "capabilities": capabilities or {},
            "clientInfo": {"name": "fauxcall", "version": __version__},
        }
        call = await self.post(protocol.request(next(self._ids), "initialize", params))
        session = call.header(protocol.SESSION_HEADER)
        if session:
            self.session_id = session
        if isinstance(call.result, dict):
            version = call.result.get("protocolVersion")
            self.negotiated_version = str(version) if version else None
        if call.status >= 400:  # noqa: PLR2004
            raise HttpStatusError(
                f"MCP initialize failed: HTTP {call.status} {call.body[:300]}".rstrip(),
                call.status,
                call.header("www-authenticate"),
            )
        if call.response is None or call.error is not None:
            raise RecordingError(f"MCP initialize failed: HTTP {call.status} {call.body[:300]}")
        if announce:
            await self.notify("notifications/initialized")
        return call

    async def call(self, method: str, params: JsonValue = None) -> RpcCall:
        """Send a request and wait for its response."""
        return await self.post(protocol.request(next(self._ids), method, params))

    async def notify(self, method: str, params: JsonValue = None) -> RpcCall:
        """Send a notification."""
        return await self.post(protocol.notification(method, params))

    async def list_all(
        self,
        method: str,
        key: str,
        *,
        before_page: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[list[JsonValue], list[RpcCall]]:
        """Follow ``nextCursor`` through a paginated list method.

        Stops at the last page, at a non-result response, or when a cursor repeats (a server bug
        that would otherwise loop). ``before_page`` runs before every request (pacing, budgets).
        """
        items: list[JsonValue] = []
        calls: list[RpcCall] = []
        cursor: JsonValue = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            if before_page is not None:
                await before_page()
            params: JsonObject | None = {"cursor": cursor} if cursor else None
            call = await self.call(method, params)
            calls.append(call)
            listing = call.result
            if not isinstance(listing, dict):
                break
            page = listing.get(key)
            if isinstance(page, list):
                items.extend(page)
            cursor = listing.get("nextCursor")
            spelled = jsonfmt.canonical_dumps(cursor)
            if not cursor or spelled in seen:
                break
            seen.add(spelled)
        return items, calls

    async def get_stream_status(self) -> tuple[int, str | None]:
        """What a GET on the endpoint does (405, or an SSE stream we close immediately)."""
        headers = {"accept": "text/event-stream", **self.headers}
        if self.session_id:
            headers[protocol.SESSION_HEADER] = self.session_id
        self._version_header(headers)
        async with self.http.stream("GET", self.url, headers=headers, timeout=10.0) as response:
            return response.status_code, response.headers.get("content-type")

    def _stream_headers(self, session: str | None) -> dict[str, str]:
        headers = {"accept": "text/event-stream", **self.headers}
        if session:
            headers[protocol.SESSION_HEADER] = session
        return self._version_header(headers)

    async def probe_http(self, http_method: str, session: str | None) -> RpcCall:
        """A GET or DELETE with ``session`` as its session id.

        A refusal is read whole; a stream the server opens is closed at once.
        """
        headers = self._stream_headers(session)
        started_ns = time.time_ns()
        started = time.perf_counter()
        async with self.http.stream(http_method, self.url, headers=headers, timeout=10.0) as answer:
            content_type = answer.headers.get("content-type")
            body = "" if _is_event_stream(content_type) else (await answer.aread()).decode()
        messages = _parse_lenient(body, content_type, answer.status_code)
        return RpcCall(
            method=http_method,
            sent=None,
            status=answer.status_code,
            request_headers=list(headers.items()),
            response_headers=list(answer.headers.multi_items()),
            content_type=content_type,
            body=body,
            messages=messages,
            response=_reply_to(messages, None),
            started_ns=started_ns,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            http_method=http_method,
        )

    async def watch_stream(self, within: float) -> tuple[RpcCall | None, RpcCall | None]:
        """Hold the client's GET stream open: its first keep-alive, and a second stream's answer.

        The second GET is for the same session, sent while the first stream is open.

        The comment comes back as a call whose body is that line and whose time is how long the
        stream stayed idle before it (``None``: none within ``within`` seconds, or no stream).
        """
        headers = self._stream_headers(self.session_id)
        started_ns = time.time_ns()
        started = time.perf_counter()
        comment: str | None = None
        async with self.http.stream(
            "GET", self.url, headers=headers, timeout=within + 10
        ) as stream:
            content_type = stream.headers.get("content-type")
            if stream.status_code != 200 or not _is_event_stream(content_type):  # noqa: PLR2004
                return None, None
            second = await self.probe_http("GET", self.session_id) if self.session_id else None
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(within):
                    async for line in stream.aiter_lines():
                        if line.startswith(":"):
                            comment = line
                            break
            elapsed = (time.perf_counter() - started) * 1000
            response_headers = list(stream.headers.multi_items())
        if comment is None:
            return None, second
        return RpcCall(
            method="GET",
            sent=None,
            status=200,
            request_headers=list(headers.items()),
            response_headers=response_headers,
            content_type=content_type,
            body=comment,
            messages=[],
            response=None,
            started_ns=started_ns,
            elapsed_ms=elapsed,
            http_method="GET",
        ), second

    async def delete_session(self) -> int | None:
        """End the session (DELETE); returns the status, or ``None`` without a session.

        The session is forgotten whatever the server answers, so it is never deleted twice.
        """
        if not self.session_id or self.transport == "sse":
            return None
        headers = self._version_header({protocol.SESSION_HEADER: self.session_id, **self.headers})
        self.session_id = None
        response = await self.http.delete(self.url, headers=headers, timeout=self.timeout_s)
        return response.status_code

    async def listen(self) -> None:
        """Open the client's GET stream and keep it open in the background.

        Every event the server sends there is kept (with when it arrived), and its requests are
        answered through ``on_request``, until :meth:`stop_listening`. Returns once the server
        has answered the GET.
        """
        if self._listening is not None:
            return
        listening = _Listening(headers=self._stream_headers(self.session_id))
        self._listening = listening
        listening.task = asyncio.create_task(self._listen(listening))
        await asyncio.wait(
            {listening.task, asyncio.ensure_future(listening.opened.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def _listen(self, listening: _Listening) -> None:
        timeout = httpx2.Timeout(self.timeout_s, read=None)  # idle streams are normal
        try:
            async with self.http.stream(
                "GET", self.url, headers=listening.headers, timeout=timeout
            ) as response:
                listening.status = response.status_code
                listening.content_type = response.headers.get("content-type")
                listening.response_headers = list(response.headers.multi_items())
                listening.opened.set()
                if not _is_event_stream(listening.content_type):
                    listening.body = (await response.aread()).decode("utf-8", "replace")
                    return
                decoder = protocol.SseDecoder()
                async for text in response.aiter_text():
                    stamp = (time.perf_counter() - listening.started) * 1000
                    for event in decoder.feed(text):
                        listening.events.append((stamp, event.event, event.data, event.id))
                        await self._answer_server(event.data)
        finally:
            listening.opened.set()

    async def stop_listening(self) -> RpcCall | None:
        """Close the GET stream :meth:`listen` opened; what the server sent there, as one call."""
        listening, self._listening = self._listening, None
        if listening is None:
            return None
        if listening.task is not None:  # (cancelling a finished task changes nothing)
            listening.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx2.HTTPError):
                await listening.task
        if listening.status is None:  # the GET failed before the server answered
            return None
        messages = [m for _, _, data, _ in listening.events for m in _json_or_nothing(data)]
        return RpcCall(
            method="GET",
            sent=None,
            status=listening.status,
            request_headers=list(listening.headers.items()),
            response_headers=listening.response_headers,
            content_type=listening.content_type,
            body=listening.body,
            messages=messages,
            response=None,
            started_ns=listening.started_ns,
            elapsed_ms=(time.perf_counter() - listening.started) * 1000,
            sse_events=listening.events,
            http_method="GET",
        )

    async def close(self) -> None:
        """Close the session and any SSE stream (safe to call more than once)."""
        with contextlib.suppress(httpx2.HTTPError):
            await self.stop_listening()
        with contextlib.suppress(httpx2.HTTPError):
            await self.delete_session()
        if self._sse_task is not None:
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx2.HTTPError):
                await self._sse_task
            self._sse_task = None
        if self._sse_stream is not None:
            stream, self._sse_stream = self._sse_stream, None
            with contextlib.suppress(httpx2.HTTPError, RuntimeError):
                await stream.__aexit__(None, None, None)

    # -- legacy HTTP+SSE transport -------------------------------------------------------------

    async def _open_sse(self) -> None:
        queue: asyncio.Queue[_QueuedEvent] = asyncio.Queue()
        self._sse_queue = queue
        self._sse_ended = False
        headers = {"accept": "text/event-stream", **self.headers}
        stream = self.http.stream(
            "GET", self.url, headers=headers, timeout=httpx2.Timeout(self.timeout_s, read=None)
        )
        response = await stream.__aenter__()
        self._sse_stream = stream
        try:
            self._sse_messages_url = await self._endpoint_of(response, queue)
        except BaseException:
            await self.close()
            raise

    async def _endpoint_of(
        self, response: httpx2.Response, queue: asyncio.Queue[_QueuedEvent]
    ) -> str:
        """Start reading the stream and wait for the ``endpoint`` event (the messages URL)."""
        if response.status_code != 200:  # noqa: PLR2004
            raise HttpStatusError(
                f"SSE transport: GET {self.url} returned {response.status_code}",
                response.status_code,
                response.headers.get("www-authenticate"),
            )
        self.sse_content_type = response.headers.get("content-type")
        self._sse_task = asyncio.create_task(self._read_sse(response, queue))
        item = await asyncio.wait_for(queue.get(), timeout=self.timeout_s)
        if item is None or item[1] != "endpoint":
            found = "the end of the stream" if item is None else repr(item[1])
            raise RecordingError(f"SSE transport: expected an 'endpoint' event, got {found}")
        return urljoin(self.url, item[2].strip())

    @staticmethod
    async def _read_sse(response: httpx2.Response, queue: asyncio.Queue[_QueuedEvent]) -> None:
        decoder = protocol.SseDecoder()
        try:
            async for text in response.aiter_text():
                for event in decoder.feed(text):
                    await queue.put((time.perf_counter(), event.event, event.data, event.id))
        finally:
            queue.put_nowait(None)

    async def _next_sse_event(self, deadline: float) -> tuple[float, str, str, str | None]:
        queue = self._sse_queue
        if queue is None or self._sse_ended:
            raise RecordingError("SSE transport: the event stream is closed")
        try:
            item = await asyncio.wait_for(queue.get(), timeout=deadline - time.perf_counter())
        except TimeoutError:
            raise RecordingError(
                f"SSE transport: no reply within {self.timeout_s:g}s on the event stream"
            ) from None
        if item is None:
            self._sse_ended = True
            raise RecordingError("SSE transport: the event stream ended before the reply")
        return item

    async def _post_sse(self, message: JsonValue) -> RpcCall:
        if self._sse_messages_url is None:
            raise RecordingError("SSE transport: call initialize() first")
        method = str(message.get("method", "")) if isinstance(message, dict) else "batch"
        headers = {"content-type": "application/json", **self.headers}
        started_ns, started = time.time_ns(), time.perf_counter()
        response = await self.http.post(
            self._sse_messages_url,
            content=jsonfmt.dumps(message).encode(),
            headers=headers,
            timeout=self.timeout_s,
        )
        wanted = message.get("id") if isinstance(message, dict) else None
        events: list[tuple[float, str, str, str | None]] = []
        reply: JsonValue = None
        messages: list[JsonValue] = []
        if wanted is not None and response.status_code < 300:  # noqa: PLR2004
            deadline = started + self.timeout_s
            while reply is None:
                at, event, data, event_id = await self._next_sse_event(deadline)
                events.append((max(0.0, (at - started) * 1000), event, data, event_id))
                if event != "message":
                    continue
                try:
                    parsed = jsonfmt.loads(data)
                except ValueError:
                    continue
                messages.append(parsed)
                if protocol.is_response(parsed) and isinstance(parsed, dict):
                    reply = parsed if parsed.get("id") == wanted else None
        return RpcCall(
            method=method,
            sent=message,
            status=response.status_code,
            request_headers=list(headers.items()),
            response_headers=list(response.headers.multi_items()),
            content_type=response.headers.get("content-type"),
            body=response.text,
            messages=messages,
            response=reply,
            started_ns=started_ns,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            sse_events=events,
        )


def _answers(event: protocol.SseEvent, wanted: JsonValue) -> bool:
    """Whether an SSE event carries the response to request ``wanted``."""
    if wanted is None or event.event != "message":
        return False
    try:
        parsed = jsonfmt.loads(event.data)
    except ValueError:
        return False
    return protocol.is_response(parsed) and isinstance(parsed, dict) and parsed.get("id") == wanted


def replier(
    replies: Mapping[str, JsonValue],
) -> Callable[[JsonObject], Awaitable[JsonValue | None]]:
    """How a client answers the server's requests (sampling, elicitation, roots …) mid-call.

    ``replies`` maps a method to its result, or to ``{"error": {...}}`` to refuse it; any other
    request gets "Method not found", as from a client without that capability.
    """

    async def reply(request: JsonObject) -> JsonValue | None:
        method = str(request.get("method"))
        found = replies.get(method)
        request_id = request.get("id")
        if isinstance(found, dict) and isinstance(found.get("error"), dict):
            error = found["error"]
            return {"jsonrpc": "2.0", "id": request_id, "error": error}
        if found is None:
            return protocol.error(request_id, protocol.METHOD_NOT_FOUND, "Method not found")
        return protocol.result(request_id, found)

    return reply
