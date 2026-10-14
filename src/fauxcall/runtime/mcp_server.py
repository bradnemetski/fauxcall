"""The fake MCP server: Streamable HTTP and legacy HTTP+SSE, reproducing the real wire behavior.

Tool definitions, capabilities, server info and instructions are served verbatim from the model;
tool calls are answered from recordings (exact arguments first, then the closest recording —
Jev when available), by custom handlers, or with the real server's error shapes.

A custom tool handler returns the tool's result. To answer the way real servers also do, it can
instead ``raise RpcError(code, message)`` (a JSON-RPC error) or raise
:class:`~fauxcall.runtime.EarlyResponse` (a whole HTTP reply, like a bare ``403`` with a
``WWW-Authenticate`` header). ``ctx.state`` holds per-session tool state that ``reset`` clears.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import itertools
import secrets
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING, Final

import jsonschema
from starlette.responses import Response, StreamingResponse

from fauxcall import jsonfmt
from fauxcall.clock import ns_to_datetime
from fauxcall.logs import bind_context, clear_context, get_logger
from fauxcall.mcpwire import protocol
from fauxcall.model.mcp import FALLBACK_PROTOCOL_VERSION, PROBE_PROMPT, PROBE_RESOURCE, PROBE_TOOL
from fauxcall.runtime.context import DEFAULT_SESSION, EarlyResponse
from fauxcall.runtime.inputs import json_inputs
from fauxcall.runtime.journal import JournalEntry
from fauxcall.runtime.matching import Candidate, match

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from starlette.requests import Request

    from fauxcall.model.mcp import McpModel, McpToolModel
    from fauxcall.model.recording import Exchange
    from fauxcall.runtime.app import FakeRuntime
    from fauxcall.runtime.context import RequestContext
    from fauxcall.types import JsonObject, JsonValue

    ToolHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]
    MethodHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]
    """``(ctx, params) -> result``, or ``None`` to fall back to the recordings."""

log = get_logger(__name__)

TOOLS_CALL: Final = "tools/call"
CLIENT_TIMEOUT_SECONDS: Final = 60.0
GET_STREAM: Final = "get"
"""The stream key of the client's GET stream (each SSE answer gets its own)."""
KEPT_EVENTS: Final = 1000
"""How many numbered events a session keeps for clients that reconnect."""
"""How long a server waits for a client's answer (the MCP SDKs' default)."""


def _is_error_answer(answer: JsonObject) -> bool:
    """A JSON-RPC error, or a tool result that reports one (``isError``)."""
    result = answer.get("result")
    return "error" in answer or (isinstance(result, dict) and result.get("isError") is True)


class _InvalidCursorError(ValueError):
    """A ``cursor`` this server never issued."""


class Channel:
    """One MCP client's way to receive messages outside any call: its open GET stream.

    Handlers reach it through ``ctx.push`` and ``ctx.background``; background work gets it as
    its argument.
    """

    def __init__(self, fake: McpFake, session: str) -> None:
        self._fake = fake
        self.session = session

    def push(self, method: str, params: JsonValue = None) -> bool:
        """Send a notification on the client's GET stream; ``False``: none is open.

        A server that numbers its events keeps one sent while no stream is open, for a client
        that reconnects with ``Last-Event-ID``.
        """
        message: JsonObject = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        stream = self._fake.streams.get(self.session)
        if stream is None:
            self._fake.keep_for_later(self.session, message)
            return False
        stream.put_nowait(message)
        return True

    async def pause(self, seconds: float) -> None:
        """Wait (the runtime's sleep, so tests can control it)."""
        await self._fake.runtime.pause(seconds)

    async def ask(
        self,
        method: str,
        params: JsonValue = None,
        *,
        within: float = CLIENT_TIMEOUT_SECONDS,
        send: Callable[[JsonValue], None] | None = None,
    ) -> JsonValue:
        """Send the client a request (``sampling/createMessage`` …) and wait for its answer.

        ``send`` delivers it (the call's SSE stream); without one it goes on the GET stream.
        Returns the client's ``result``; raises :class:`ClientError` with its ``error``, or when
        it can't be reached or doesn't answer ``within`` seconds (``-32001 Request timed
        out``, as the SDKs say).
        """
        request_id = f"fauxcall-{next(self._fake.request_ids)}"
        message = protocol.request(request_id, method, params)
        if send is None:
            stream = self._fake.streams.get(self.session)
            if stream is None:
                raise ClientError(protocol.REQUEST_TIMEOUT, "Request timed out")
            send = stream.put_nowait
        waiting: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        key = (self.session, request_id)
        self._fake.awaiting[key] = waiting
        try:
            send(message)
            async with asyncio.timeout(within):
                answer = await waiting
        except TimeoutError as error:
            raise ClientError(protocol.REQUEST_TIMEOUT, "Request timed out") from error
        finally:
            self._fake.awaiting.pop(key, None)
        failure = answer.get("error")
        if isinstance(failure, dict):
            raise ClientError(
                int(failure.get("code", protocol.INTERNAL_ERROR)),  # type: ignore[arg-type]
                str(failure.get("message", "")),
                failure.get("data"),
            )
        return answer.get("result")

    def background(self, work: Callable[[Channel], Awaitable[None]]) -> asyncio.Task[None]:
        """Run ``work(self)`` until it ends, is cancelled, or the client's session ends."""
        task = asyncio.ensure_future(work(self))
        tasks = self._fake.background.setdefault(self.session, set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task


class RpcError(Exception):
    """Raised by a tool handler to answer the call with a JSON-RPC error instead of a result."""

    def __init__(self, code: int, message: str, data: JsonValue = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class ClientError(RpcError):
    """The client refused a request the server sent it, or never answered.

    Raise it on (or let it propagate) to answer the call with the same error, as SDK servers do
    (``MCP error -32001: Request timed out``).
    """


_LISTINGS: Final = frozenset(
    {"tools/list", "resources/list", "resources/templates/list", "prompts/list"}
)
_ACKNOWLEDGED: Final = frozenset({"ping", "logging/setLevel"})
"""Methods answered with an empty result unless a handler or a recording says otherwise."""
_SUBSCRIPTIONS: Final = frozenset({"resources/subscribe", "resources/unsubscribe"})
_NAMED_TARGETS: Final = frozenset({"resources/read", "prompts/get"})
"""Methods about one named resource or prompt: an unknown one gets the real "not found" error."""

_PROBES: Final = {
    "unknown_tool": PROBE_TOOL,
    "resources/read_not_found": PROBE_RESOURCE,
    "prompts/get_not_found": PROBE_PROMPT,
}
"""Situations whose recorded error names the probe introspection asked for."""


def _renamed(value: JsonValue, probe: str, actual: str) -> JsonValue:
    """``value`` with every mention of ``probe`` in its strings replaced by ``actual``."""
    if isinstance(value, str):
        return value.replace(probe, actual)
    if isinstance(value, list):
        return [_renamed(item, probe, actual) for item in value]
    if isinstance(value, dict):
        return {key: _renamed(item, probe, actual) for key, item in value.items()}
    return value


def tool_endpoint(name: str) -> str:
    """Endpoint id under which calls of one tool are recorded."""
    return f"MCP {TOOLS_CALL}:{name}"


def method_endpoint(method: str, detail: str | None = None) -> str:
    """Endpoint id for other MCP methods (``MCP resources/read:<uri>`` …)."""
    return f"MCP {method}" + (f":{detail}" if detail else "")


def flatten_args(arguments: JsonValue) -> dict[str, JsonValue]:
    """Tool arguments as matching inputs: ``{"c": [2, 3]}`` -> ``{"args.c[*]": [2, 3]}``."""
    return json_inputs("args", arguments)


class McpFake:
    """Answers MCP traffic for one fake."""

    def __init__(self, runtime: FakeRuntime, model: McpModel) -> None:
        self.runtime = runtime
        self.model = model
        self.transport = model.transport
        self.tool_handlers: dict[str, ToolHandler] = {}
        self.method_handlers: dict[str, MethodHandler] = {}
        self._sessions: dict[str, str] = {}
        self._capabilities: dict[str, JsonObject] = {}
        self._sent: dict[str, deque[tuple[str, str, JsonValue]]] = {}
        """MCP session -> the numbered events sent to it: (stream, event id, message)."""
        self._versions: dict[str, str] = {}
        """MCP session -> the protocol version it negotiated."""
        self.streams: dict[str, asyncio.Queue[JsonValue]] = {}
        """MCP session -> its open GET stream (messages pushed outside any call)."""
        self.background: dict[str, set[asyncio.Task[None]]] = {}
        self.awaiting: dict[tuple[str, str], asyncio.Future[JsonObject]] = {}
        """(MCP session, request id) -> the server's request waiting for the client's answer."""
        self.request_ids = itertools.count(1)
        """MCP session -> work handlers left running after a call (``ctx.background``)."""
        """MCP session -> the capabilities its client declared in ``initialize``."""
        self._sse_queues: dict[str, asyncio.Queue[str]] = {}

    # -- helpers ---------------------------------------------------------------------------------

    def _negotiate(self, requested: JsonValue) -> str:
        mapping = self.transport.protocol_versions
        if isinstance(requested, str) and requested in mapping:
            return mapping[requested]
        if self.transport.default_protocol_version is not None:  # modeled from the real server
            return self.transport.default_protocol_version
        result = self.model.initialize_result
        if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str):
            return str(result["protocolVersion"])
        return FALLBACK_PROTOCOL_VERSION

    def _error(
        self,
        request_id: JsonValue,
        situation: str,
        code: int,
        message: str,
        *,
        target: str | None = None,
    ) -> JsonObject:
        """The real server's error for ``situation``, naming the caller's ``target`` in it."""
        recorded = self.model.errors.get(situation)
        if isinstance(recorded, dict):
            probe = _PROBES.get(situation)
            if probe is not None and target is not None:
                recorded = _renamed(recorded, probe, target)
            return protocol.with_id(recorded, request_id)
        return protocol.error(request_id, code, message)

    async def _recorded_result(
        self,
        endpoint_id: str,
        inputs: dict[str, JsonValue],
        tool: McpToolModel | None,
    ) -> JsonValue | None:
        """The recording that answers best: exact inputs first, then the closest.

        Tool calls are matched by their arguments (``args.*``), the other methods (prompts,
        resource reads, completions) by their whole params (``params.*``), as ``inputs`` are.
        """
        exchanges = [
            e for e in self.runtime.recordings.for_endpoint(endpoint_id) if e.response is not None
        ]
        if not exchanges:
            return None
        candidates = []
        replies: dict[str, JsonValue] = {}
        for exchange in exchanges:
            sent = exchange.request.body.parsed()
            params = sent.get("params") if isinstance(sent, dict) else None
            arguments = params.get("arguments") if isinstance(params, dict) else None
            recorded = (
                flatten_args(arguments)
                if tool is not None
                else json_inputs("params", params if isinstance(params, dict) else {})
            )
            candidates.append(Candidate(exchange.id, recorded))
            assert exchange.response is not None  # noqa: S101
            replies[exchange.id] = exchange.response.body.parsed()
        spec = tool.semantic.match if tool is not None and tool.semantic is not None else None
        chosen = await match(inputs, candidates, spec, self.runtime.jev)
        if chosen.candidate is None:
            return None
        if chosen.method != "exact":
            log.info(
                "mcp_tool_matched",
                endpoint=endpoint_id,
                method=chosen.method,
                chosen=chosen.candidate.id,
            )
        return replies[chosen.candidate.id]

    # -- method handlers -------------------------------------------------------------------------

    async def respond_to(self, ctx: RequestContext, message: JsonObject) -> JsonObject:
        """The JSON-RPC response for one request message."""
        request_id = message.get("id")
        method = str(message.get("method"))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        assert isinstance(params, dict)  # noqa: S101
        try:
            return await self._dispatch(ctx, request_id, method, params)
        except _InvalidCursorError:
            return self._error(
                request_id, "invalid_cursor", protocol.INVALID_PARAMS, "Invalid cursor"
            )

    async def _dispatch(
        self, ctx: RequestContext, request_id: JsonValue, method: str, params: JsonObject
    ) -> JsonObject:
        match method:
            case "initialize":
                body = (
                    dict(self.model.initialize_result)
                    if isinstance(self.model.initialize_result, dict)
                    else {}
                )
                body["protocolVersion"] = self._negotiate(params.get("protocolVersion"))
                return protocol.result(request_id, body)
            case _ if method in _LISTINGS and method in self.method_handlers:
                handler = self.method_handlers[method]
                answer = await self._handled(request_id, handler(ctx, params))
                return answer or await self._dispatch_listing(ctx, request_id, method, params)
            case "tools/list" | "resources/list" | "resources/templates/list" | "prompts/list":
                return await self._dispatch_listing(ctx, request_id, method, params)
            case "tools/call":
                return await self._call_tool(ctx, request_id, params)
            case _:
                return await self._other_method(ctx, request_id, method, params)

    async def _dispatch_listing(
        self, ctx: RequestContext, request_id: JsonValue, method: str, params: JsonObject
    ) -> JsonObject:
        """A recorded listing, paginated as recorded (tools: only those this client is offered)."""
        match method:
            case "tools/list":
                tools = [
                    t.definition for t in self.model.tools if t.offered_to(ctx.client_capabilities)
                ]
                return self._listing(request_id, method, self._page(params, tools, "tools"))
            case "resources/list":
                page = self._page(params, self.model.resources, "resources")
                return self._listing(request_id, method, page)
            case "resources/templates/list":
                page = self._page(params, self.model.resource_templates, "resourceTemplates")
                return self._listing(request_id, method, page)
            case _:
                page = self._page(params, self.model.prompts, "prompts")
                return self._listing(request_id, method, page)

    async def _other_method(
        self, ctx: RequestContext, request_id: JsonValue, method: str, params: JsonObject
    ) -> JsonObject:
        """Any other method: its custom handler, then a recording, then the protocol's default.

        A handler (``register_method``) may return ``None`` to leave a call to the recordings, so
        it can compute dynamic resources and let the recorded static ones replay.
        """
        handler = self.method_handlers.get(method)
        if handler is not None:
            answer = await self._handled(request_id, handler(ctx, params))
            if answer is not None:
                return answer
        detail = params.get("uri") or params.get("name")
        endpoint_id = method_endpoint(method, str(detail) if detail else None)
        recorded = await self._recorded_result(endpoint_id, json_inputs("params", params), None)
        if isinstance(recorded, dict):
            return protocol.with_id(recorded, request_id)
        if method in _ACKNOWLEDGED or (method in _SUBSCRIPTIONS and self._subscribable()):
            return protocol.result(request_id, {})
        if method == "completion/complete" and self._capability("completions") is not None:
            return protocol.result(request_id, {"completion": {"values": [], "hasMore": False}})
        if method in _NAMED_TARGETS:
            return self._error(
                request_id,
                f"{method}_not_found",
                protocol.INVALID_PARAMS,
                "Not found",
                target=str(detail) if detail else None,
            )
        return self._error(
            request_id, "method_not_found", protocol.METHOD_NOT_FOUND, "Method not found"
        )

    def _capability(self, name: str) -> JsonValue:
        """A capability the server advertised in ``initialize`` (``None``: not advertised)."""
        result = self.model.initialize_result
        capabilities = result.get("capabilities") if isinstance(result, dict) else None
        return capabilities.get(name) if isinstance(capabilities, dict) else None

    def _subscribable(self) -> bool:
        """Whether the server advertised ``resources.subscribe``."""
        resources = self._capability("resources")
        return isinstance(resources, dict) and resources.get("subscribe") is True

    async def _handled(
        self, request_id: JsonValue, pending: Awaitable[JsonValue]
    ) -> JsonObject | None:
        """A custom handler's answer: its result, its ``RpcError``, or an internal error.

        A handler that crashes answers like an SDK server does (``-32603``), never with a bare
        HTTP 500. ``None`` means the handler left the call to the recordings.
        """
        try:
            value = await pending
        except RpcError as error:
            return protocol.error(request_id, error.code, error.message, error.data)
        except EarlyResponse:
            raise
        except Exception as error:  # any bug in custom code becomes -32603
            log.exception("mcp_handler_failed")
            return protocol.error(
                request_id, protocol.INTERNAL_ERROR, str(error) or "Internal error"
            )
        return None if value is None else protocol.result(request_id, value)

    def _listing(self, request_id: JsonValue, method: str, page: JsonObject) -> JsonObject:
        """A listing page with the real result's other members, in the recorded order."""
        extras = self.model.listing_extras.get(method)
        if not isinstance(extras, dict):
            return protocol.result(request_id, page)
        body: JsonObject = {name: page.get(name, value) for name, value in extras.items()}
        body.update({name: value for name, value in page.items() if name not in body})
        return protocol.result(request_id, body)

    def _page(self, params: JsonObject, items: list[JsonValue], key: str) -> JsonObject:
        size = self.model.tools_page_size if key == "tools" else None
        if size is None:
            return {key: items}
        cursor = params.get("cursor")
        try:
            start = int(base64.urlsafe_b64decode(str(cursor)).decode()) if cursor else 0
        except (ValueError, binascii.Error) as exc:
            raise _InvalidCursorError(str(cursor)) from exc
        if start < 0 or start > len(items):
            raise _InvalidCursorError(str(cursor))
        page: JsonObject = {key: items[start : start + size]}
        if start + size < len(items):
            page["nextCursor"] = base64.urlsafe_b64encode(str(start + size).encode()).decode()
        return page

    async def _call_tool(
        self, ctx: RequestContext, request_id: JsonValue, params: JsonObject
    ) -> JsonObject:
        named = params.get("name")
        if not isinstance(named, str) or not named:  # the real refusal, when recorded
            return self._error(
                request_id,
                "tool_call_without_name",
                protocol.INVALID_PARAMS,
                "Invalid params: name is required",
            )
        name = named
        sent = params.get("arguments", {})
        tool = self.model.tool(name)
        if tool is None or not tool.offered_to(ctx.client_capabilities):
            return self._error(
                request_id,
                "unknown_tool",
                protocol.INVALID_PARAMS,
                f"Unknown tool: {name}",
                target=name,
            )
        if not isinstance(sent, dict):  # null, a list, a string: not arguments at all
            return await self._odd_arguments(request_id, tool, sent)
        arguments = sent
        started = time.perf_counter()
        definition = tool.definition if isinstance(tool.definition, dict) else {}
        schema = definition.get("inputSchema")
        if isinstance(schema, dict) and tool.invalid_args != "accept":
            try:
                jsonschema.validate(jsonfmt.plain(arguments), schema)
            except jsonschema.ValidationError as exc:
                refused = self._invalid_args(request_id, tool, exc.message)
                await self._tool_latency(ctx, tool, refused, started, refused_arguments=True)
                return refused
        try:
            answer = await self._tool_answer(ctx, request_id, tool, arguments)
        except EarlyResponse:  # an HTTP-level refusal takes the tool's error time too
            refusal = protocol.error(request_id, protocol.INTERNAL_ERROR, "refused")
            await self._tool_latency(ctx, tool, refusal, started)
            raise
        await self._tool_latency(ctx, tool, answer, started)
        return answer

    async def _odd_arguments(
        self, request_id: JsonValue, tool: McpToolModel, sent: JsonValue
    ) -> JsonObject:
        """Non-object ``arguments``: the answer recorded for exactly them, else invalid params."""
        wanted = jsonfmt.canonical_dumps(sent)
        for exchange in self.runtime.recordings.for_endpoint(tool_endpoint(tool.name)):
            reply = exchange.response.body.parsed() if exchange.response else None
            if _sent_arguments(exchange) == wanted and isinstance(reply, dict):
                return protocol.with_id(reply, request_id)
        recorded = self.model.errors.get("arguments_not_object")
        probe = self.model.arguments_probe_tool
        if isinstance(recorded, dict) and probe:
            return protocol.with_id(_renamed(recorded, probe, tool.name), request_id)
        return protocol.error(
            request_id, protocol.INVALID_PARAMS, "Invalid params: arguments must be an object"
        )

    async def _tool_latency(
        self,
        ctx: RequestContext,
        tool: McpToolModel,
        answer: JsonObject,
        started: float,
        *,
        refused_arguments: bool = False,
    ) -> None:
        """Take the tool's recorded time for this outcome, counting the time already spent.

        Errors take the error times when they were recorded; a call refused for its arguments
        is answered at once unless they were (how fast the server refuses was never measured).
        """
        latency = tool.latency
        if latency is None or not self.runtime.settings.latency_enabled:
            return
        if refused_arguments and not latency.error_samples_ms:
            return
        target_ms = latency.pick_ms(ctx.rng, error=_is_error_answer(answer))
        if target_ms is None:
            return
        remaining = target_ms / 1000 - (time.perf_counter() - started)
        if remaining > 0:
            await self.runtime.pause(remaining)

    async def _tool_answer(
        self, ctx: RequestContext, request_id: JsonValue, tool: McpToolModel, arguments: JsonObject
    ) -> JsonObject:
        """The tool's answer: its handler's, the closest recording's, or the unrecorded one."""
        name = tool.name
        handler = self.tool_handlers.get(name)
        if handler is not None or tool.behavior == "computed":
            if handler is None:
                return protocol.error(
                    request_id, protocol.INTERNAL_ERROR, f"fauxcall: no handler for {name}"
                )
            answer = await self._handled(request_id, handler(ctx, arguments))
            return answer if answer is not None else protocol.result(request_id, None)
        recorded = await self._recorded_result(tool_endpoint(name), flatten_args(arguments), tool)
        if isinstance(recorded, dict):
            return protocol.with_id(recorded, request_id)
        return self._unrecorded(request_id, tool)

    def _unrecorded(self, request_id: JsonValue, tool: McpToolModel) -> JsonObject:
        """The answer when no recording fits the call."""
        spec = tool.semantic.match if tool.semantic is not None else None
        if spec is not None and spec.no_match == "empty":
            return protocol.result(request_id, {"content": []})
        if tool.behavior == "error":  # a tool that always fails, without a recorded failure
            template = tool.invalid_template
            if isinstance(template, dict) and ("error" in template or "result" in template):
                return protocol.with_id(template, request_id)
            content: JsonValue = [{"type": "text", "text": f"Error executing tool {tool.name}"}]
            return protocol.result(request_id, {"content": content, "isError": True})
        return self._error(
            request_id, "no_recording", protocol.INTERNAL_ERROR, "No matching result"
        )

    def _invalid_args(self, request_id: JsonValue, tool: McpToolModel, detail: str) -> JsonObject:
        template = tool.invalid_template
        if isinstance(template, dict) and ("error" in template or "result" in template):
            return protocol.with_id(template, request_id)
        if tool.invalid_args == "jsonrpc_error":
            return protocol.error(
                request_id, protocol.INVALID_PARAMS, f"Invalid arguments: {detail}"
            )
        content: JsonValue = [{"type": "text", "text": f"Invalid arguments: {detail}"}]
        return protocol.result(request_id, {"content": content, "isError": True})

    # -- HTTP transport ---------------------------------------------------------------------------

    def _reply(
        self, messages: list[JsonValue], headers: dict[str, str], session: str = ""
    ) -> Response:
        if self.transport.response_mode == "sse":
            stream = secrets.token_hex(4)
            priming = self._priming(session, stream)  # first, as it goes out first
            events = b"".join(self._event(m, session, stream) for m in messages)
            return Response(
                content=priming + events,
                media_type=self.transport.content_type_sse,
                headers=headers,
            )
        ordered = [self._ordered(m) for m in messages]
        payload: JsonValue = ordered[0] if len(ordered) == 1 else ordered
        return Response(
            content=jsonfmt.dumps(payload).encode(),
            media_type=self.transport.content_type_json,
            headers=headers,
        )

    async def streamable(self, request: Request) -> Response:
        """Handle GET/POST/DELETE on the Streamable HTTP endpoint."""
        if request.method in {"GET", "DELETE"}:
            return self._get_or_delete(request)
        refused = self._http_refusal(request)
        if refused is not None:
            return refused
        messages = _messages(await request.body())
        if isinstance(messages, Response):
            return messages
        headers, refused = self._session_of(request, messages)
        if refused is not None:
            return refused
        session = (
            headers.get(self.transport.session_header)
            or request.headers.get(self.transport.session_header)
            or ""
        )
        self._resolve(session, messages)
        self._notified(request, messages)
        requests = [m for m in messages if protocol.is_request(m) and isinstance(m, dict)]
        if not requests:
            return Response(status_code=202)
        streaming = self.transport.response_mode == "sse"
        try:
            if streaming and len(requests) == 1:
                return await self._answer_streaming(request, requests[0], headers, session)
            sent: list[JsonValue] = []
            for message in requests:
                notes, reply = await self._answer(request, message)
                sent += [*notes, reply] if streaming else [reply]
        except EarlyResponse as early:  # a tool answered the whole HTTP request
            return early.response
        return self._reply(sent, headers, session)

    def _http_refusal(self, request: Request) -> Response | None:
        """The real server's HTTP-level refusal of this POST, if it would refuse it."""
        headers = request.headers
        accept = headers.get("accept", "")
        situations = {
            "accept": "text/event-stream" not in accept or "application/json" not in accept,
            "content_type": "application/json" not in headers.get("content-type", ""),
            "protocol_header": headers.get(protocol.PROTOCOL_HEADER, "")
            not in {
                "",
                *protocol.KNOWN_VERSIONS,
                *self.transport.protocol_versions,
            },
            "unknown_session": bool(headers.get(self.transport.session_header))
            and headers[self.transport.session_header] not in self._sessions,
        }
        for situation, applies in situations.items():
            refused = self._refusal(situation) if applies else None
            if refused is not None:
                return refused
        return None

    def _refusal(self, situation: str) -> Response | None:
        """The real server's recorded HTTP refusal in ``situation``, if introspection saw one."""
        recorded = self.transport.refusals.get(situation)
        if not isinstance(recorded, dict):
            return None
        status = recorded.get("status")
        return Response(
            content=str(recorded.get("body") or "").encode(),
            status_code=status if isinstance(status, int) else 400,
            media_type=str(recorded.get("content_type") or "text/plain"),
        )

    def _ordered(self, message: JsonValue) -> JsonValue:
        """A response with its members in the order the real server writes them."""
        order = self.transport.member_order
        if not order or not isinstance(message, dict) or "method" in message:
            return message
        rank = {name: index for index, name in enumerate(order)}
        return dict(sorted(message.items(), key=lambda item: rank.get(item[0], len(rank))))

    def _event(
        self,
        message: JsonValue,
        session: str = "",
        stream: str = "",
        *,
        event_id: str | None = None,
    ) -> bytes:
        """One SSE event, with an id shaped like the real server's when it sends them.

        A numbered event is kept for the session (``stream`` names the SSE response it went
        on), so a client that reconnects with ``Last-Event-ID`` gets what followed it.
        """
        numbered = self.transport.sse_event_ids
        event_id = event_id or self._new_id(self.transport.event_id_format, 8)
        if numbered and session:
            self._kept(session).append((stream, event_id, message))
        return protocol.sse_event(
            self._ordered(message), event_id=event_id if numbered else None
        ).encode()

    def _priming(self, session: str, stream: str) -> bytes:
        """The empty event newer protocol versions open SSE answers with, when the server does."""
        if self._versions.get(session) not in self.transport.priming_versions:
            return b""
        event_id = self._new_id(self.transport.event_id_format, 8)
        if session:
            self._kept(session).append((stream, event_id, None))  # a client may resume from it
        return f"id: {event_id}\ndata: \n\n".encode()

    def _kept(self, session: str) -> deque[tuple[str, str, JsonValue]]:
        return self._sent.setdefault(session, deque(maxlen=KEPT_EVENTS))

    def keep_for_later(self, session: str, message: JsonValue) -> None:
        """A message for the client's GET stream while none is open, kept to replay later.

        Only a server that numbers its events can replay them; others drop it, as they do.
        """
        if self.transport.sse_event_ids and session:
            event_id = self._new_id(self.transport.event_id_format, 8)
            self._kept(session).append((GET_STREAM, event_id, message))

    def _after(self, session: str, last_event_id: str) -> list[tuple[str, JsonValue]]:
        """The events that followed ``last_event_id`` on its stream: (event id, message)."""
        kept = list(self._sent.get(session, ()))
        found = next((i for i, (_, eid, _) in enumerate(kept) if eid == last_event_id), None)
        if found is None:
            return []
        stream = kept[found][0]
        return [(eid, m) for s, eid, m in kept[found + 1 :] if s == stream and m is not None]

    @staticmethod
    def _new_id(shape: str, size: int) -> str:
        """A fresh id shaped like the real server's (a UUID, or hex)."""
        return str(uuid.uuid4()) if shape == "uuid" else secrets.token_hex(size)

    def _get_or_delete(self, request: Request) -> Response:
        """A GET (the server's stream, or its refusal) or a DELETE (ends the session)."""
        session = request.headers.get(self.transport.session_header)
        unknown = bool(session) and session not in self._sessions
        if request.method == "DELETE":
            refused = self._refusal("delete_unknown_session") if unknown else None
            if refused is not None:
                return refused
            if session:
                self._end(session)
            return Response(status_code=self.transport.delete_status)
        if self.transport.get_stream == "status":
            status = self.transport.get_status
            allow = {"allow": "POST, DELETE"} if status == 405 else {}  # noqa: PLR2004
            return Response(status_code=status, headers=allow)
        situation = (
            "get_unknown_session"
            if unknown
            else "get_second_stream"
            if session in self.streams
            else None
        )
        refused = self._refusal(situation) if situation else None
        if refused is not None:
            return refused
        queue: asyncio.Queue[JsonValue] = asyncio.Queue()
        self.streams[session or ""] = queue  # open from now: a second GET is refused at once
        missed = self._after(session or "", request.headers.get("last-event-id", ""))
        return StreamingResponse(
            self._server_stream(session or "", queue, missed),
            media_type=self.transport.content_type_sse,
        )

    def _resolve(self, session: str, messages: Sequence[JsonValue]) -> None:
        """Hand the client's answers to the requests the server sent it."""
        for message in messages:
            if protocol.is_response(message) and isinstance(message, dict):
                waiting = self.awaiting.get((session, str(message.get("id"))))
                if waiting is not None and not waiting.done():
                    waiting.set_result(message)

    def _notified(self, request: Request, messages: Sequence[JsonValue]) -> None:
        """Run the handlers of the client's notifications (``register_method``).

        Each runs in the background, so the client gets its ``202`` at once, as from a real
        server; it may ask the client things (``roots/list`` after
        ``notifications/roots/list_changed``), and ends with the client's session.
        """
        for message in messages:
            if not (protocol.is_notification(message) and isinstance(message, dict)):
                continue
            handler = self.method_handlers.get(str(message.get("method")))
            if handler is None:
                continue
            ctx = self._context(request, message)
            params = message.get("params")
            work = handler(ctx, params if isinstance(params, dict) else {})
            assert ctx.channel is not None  # noqa: S101 - _context always gives one
            ctx.channel.background(_quietly(work))

    def _end(self, session: str) -> None:
        """A client ended its session: forget it and stop what it left running."""
        self._sessions.pop(session, None)
        self._capabilities.pop(session, None)
        self._sent.pop(session, None)
        self.streams.pop(session, None)
        for task in self.background.pop(session, set()):
            task.cancel()

    def _keep_alive(self) -> bytes:
        """The comment the real server writes to keep an idle SSE stream open."""
        return f"{self.transport.keep_alive}\n\n".encode()

    async def _idle(self) -> None:
        """Wait out the real server's keep-alive interval.

        Wall-clock time, like the real server's timer: not a pause, so it is never skipped or
        noted in ``runtime.pauses``.
        """
        await asyncio.sleep(self.transport.keep_alive_seconds)

    async def _server_stream(
        self,
        session: str,
        queue: asyncio.Queue[JsonValue],
        missed: Sequence[tuple[str, JsonValue]] = (),
    ) -> AsyncIterator[bytes]:
        """The client's GET stream: pushed notifications, and comments that keep it open.

        A client reconnecting with ``Last-Event-ID`` first gets what it missed.
        """
        for event_id, message in missed:  # with their ids: the client may reconnect again
            yield self._event(message, event_id=event_id)
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                idle = asyncio.ensure_future(self._idle())
                await asyncio.wait({getter, idle}, return_when=asyncio.FIRST_COMPLETED)
                if getter.done():
                    idle.cancel()
                    yield self._event(getter.result(), session, GET_STREAM)
                else:
                    getter.cancel()
                    yield self._keep_alive()
        finally:
            if self.streams.get(session) is queue:
                del self.streams[session]

    def _session_of(
        self, request: Request, messages: Sequence[JsonValue]
    ) -> tuple[dict[str, str], Response | None]:
        """A new session's header for an ``initialize``; the refusal of a missing session."""
        session_header = self.transport.session_header
        session = request.headers.get(session_header)
        is_init = any(isinstance(m, dict) and m.get("method") == "initialize" for m in messages)
        again = self.model.errors.get("second_initialize")
        if is_init and session in self._sessions and isinstance(again, dict):
            init = next(
                m for m in messages if isinstance(m, dict) and m.get("method") == "initialize"
            )
            refused = Response(
                content=jsonfmt.dumps(protocol.with_id(again, init.get("id"))).encode(),
                status_code=self.transport.reinitialize_status or 400,
                media_type="application/json",
            )
            return {}, refused
        if is_init and self.transport.issues_session:
            session = self._new_id(self.transport.session_id_format, 16)
            init = next(
                m for m in messages if isinstance(m, dict) and m.get("method") == "initialize"
            )
            params = init.get("params")
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            self._versions[session] = self._negotiate(requested)
            self._sessions[session] = (
                request.headers.get(self.runtime.settings.session_header) or DEFAULT_SESSION
            )
            self._capabilities[session] = _declared(messages)
            return {session_header: session}, None
        if (
            self.transport.requires_session
            and self.transport.issues_session
            and (not session or session not in self._sessions)
        ):
            missing: JsonValue = self.model.errors.get("missing_session") or protocol.error(
                None, protocol.INVALID_REQUEST, "Bad Request: No valid session ID provided"
            )
            refused = Response(
                content=jsonfmt.dumps(missing).encode(),
                status_code=self.transport.missing_session_status,
                media_type="application/json",
            )
            return {}, refused
        return {}, None

    async def _answer_streaming(
        self, request: Request, message: JsonObject, headers: dict[str, str], session: str = ""
    ) -> Response:
        """One request over SSE: notifications reach the client as they happen, then the answer.

        Until the first notification nothing is sent, so an answer without any (or an HTTP-level
        refusal) still goes out as one plain response.
        """
        queue: asyncio.Queue[JsonValue] = asyncio.Queue()
        task = asyncio.create_task(self._answer(request, message, on_notify=queue.put_nowait))
        waiting = asyncio.create_task(queue.get())
        idle = asyncio.create_task(self._idle())
        await asyncio.wait({task, waiting, idle}, return_when=asyncio.FIRST_COMPLETED)
        idle.cancel()
        if task.done():
            early = [waiting.result()] if waiting.done() else []
            waiting.cancel()
            notes, reply = task.result()  # raises the EarlyResponse of a refusal
            return self._reply([*early, *_drain(queue), *notes, reply], headers, session)
        stream = secrets.token_hex(4)
        priming = self._priming(session, stream)  # first, as it goes out first
        if waiting.done():
            opening = self._event(waiting.result(), session, stream)
        else:  # a long call: the stream opens with a keep-alive, as the real server's does
            waiting.cancel()
            opening = self._keep_alive()

        async def events() -> AsyncIterator[bytes]:
            try:
                yield priming + opening
                while True:
                    getter = asyncio.create_task(queue.get())
                    idle = asyncio.create_task(self._idle())
                    await asyncio.wait({task, getter, idle}, return_when=asyncio.FIRST_COMPLETED)
                    idle.cancel()
                    if getter.done():
                        yield self._event(getter.result(), session, stream)
                        continue
                    getter.cancel()
                    if task.done():
                        break
                    yield self._keep_alive()
                try:  # every notification was streamed: a finished task left none behind
                    _, reply = task.result()
                except EarlyResponse:  # too late for an HTTP status: the stream has started
                    reply = protocol.error(message.get("id"), protocol.INTERNAL_ERROR, "refused")
                yield self._event(reply, session, stream)
            finally:
                if not task.done():  # pragma: no cover - the client left mid-stream
                    task.cancel()

        return StreamingResponse(
            events(), media_type=self.transport.content_type_sse, headers=headers
        )

    def _context(self, request: Request, message: JsonObject) -> RequestContext:
        """The context a handler gets for one message from the client."""
        ctx = self.runtime.new_context(request, None)
        ctx.identity = getattr(request.state, "fauxcall_identity", None)
        ctx.json_body = message
        ctx.mcp_session = request.headers.get(self.transport.session_header) or _legacy_session(
            request
        )
        ctx.client_capabilities = dict(self._capabilities.get(ctx.mcp_session or "", {}))
        ctx.channel = Channel(self, ctx.mcp_session or "")
        return ctx

    async def _answer(
        self,
        request: Request,
        message: JsonObject,
        on_notify: Callable[[JsonValue], None] | None = None,
    ) -> tuple[list[JsonValue], JsonObject]:
        """One request's reply, and the notifications its handler sent (unless streamed)."""
        started = time.perf_counter()
        method = str(message.get("method"))
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        endpoint_id = tool_endpoint(str(name)) if method == TOOLS_CALL else method_endpoint(method)
        ctx = self._context(request, message)
        ctx.on_notify = on_notify
        bind_context(request_id=ctx.request_id, session=ctx.session, endpoint=endpoint_id)
        fault = self.runtime.faults.pick(endpoint_id, ctx.session)
        if fault is not None and fault.kind != "delay":
            reply = protocol.error(
                message.get("id"), protocol.INTERNAL_ERROR, f"fauxcall injected {fault.kind}"
            )
        else:
            if fault is not None:
                await self.runtime.pause(fault.delay_ms / 1000)
            try:
                reply = await self.respond_to(ctx, message)
            except EarlyResponse as early:
                self._journal(ctx, request, endpoint_id, message, None, started, None, early)
                clear_context()
                raise
        self._journal(
            ctx, request, endpoint_id, message, reply, started, fault.kind if fault else None
        )
        clear_context()
        return list(ctx.notifications), reply

    def _journal(
        self,
        ctx: RequestContext,
        request: Request,
        endpoint_id: str,
        message: JsonObject,
        reply: JsonObject | None,
        started: float,
        fault: str | None,
        early: EarlyResponse | None = None,
    ) -> None:
        if not self.runtime.journal.enabled:
            return
        response_headers: list[tuple[str, str]] = []
        response_text: str | None = None
        if early is not None:
            response_headers = self.runtime.redact(early.response.headers.items())
            body = getattr(early.response, "body", b"")
            response_text = bytes(body).decode("utf-8", "replace") or None
        self.runtime.journal.add(
            JournalEntry(
                seq=ctx.seq,
                session=ctx.session,
                request_id=ctx.request_id,
                at=ns_to_datetime(ctx.now_ns),
                method=request.method,
                path=request.url.path,
                endpoint=endpoint_id,
                request_headers=self.runtime.redact(request.headers.items()),
                request_body=message,
                status=early.response.status_code if early is not None else 200,
                response_headers=response_headers,
                response_body=reply,
                response_text=response_text,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                fault=fault,
                identity=ctx.identity.name if ctx.identity else None,
                mcp_method=str(message.get("method")),
            )
        )

    # -- legacy HTTP+SSE transport -----------------------------------------------------------------

    async def sse_stream(self, request: Request) -> Response:
        """GET on the SSE endpoint: announce the messages URL, then stream responses."""
        session = uuid.uuid4().hex
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._sse_queues[session] = queue
        self._sessions[session] = (
            request.headers.get(self.runtime.settings.session_header) or DEFAULT_SESSION
        )
        messages_path = self.transport.messages_path or "/messages/"

        async def events() -> AsyncIterator[bytes]:
            yield f"event: endpoint\ndata: {messages_path}?session_id={session}\n\n".encode()
            try:
                while True:  # until the client disconnects
                    yield (await queue.get()).encode()
            finally:
                self._sse_queues.pop(session, None)

        return StreamingResponse(events(), media_type=self.transport.content_type_sse)

    async def sse_message(self, request: Request) -> Response:
        """POST to the messages URL: answer over the session's SSE stream."""
        session = request.query_params.get("session_id")
        if session is None:
            return Response(content=b"session_id is required", status_code=400)
        try:
            session = uuid.UUID(hex=session).hex
        except ValueError:
            return Response(content=b"Invalid session ID", status_code=400)
        queue = self._sse_queues.get(session)
        if queue is None:
            return Response(content=b"Could not find session", status_code=404)
        try:
            parsed = jsonfmt.loads(await request.body())
        except ValueError:
            return Response(content=b"Could not parse message", status_code=400)
        messages = parsed if isinstance(parsed, list) else [parsed]
        self._resolve(session, messages)
        self._notified(request, messages)
        for message in messages:
            if protocol.is_request(message) and isinstance(message, dict):

                def stream(note: JsonValue, queue: asyncio.Queue[str] = queue) -> None:
                    queue.put_nowait(protocol.sse_event(note))

                try:
                    _, reply = await self._answer(request, message, on_notify=stream)
                except EarlyResponse as early:  # a tool answered the POST itself
                    return early.response
                await queue.put(protocol.sse_event(reply))
        return Response(content=b"Accepted", status_code=202)


def _quietly(work: Awaitable[JsonValue]) -> Callable[[Channel], Awaitable[None]]:
    """A notification handler's work: it has no one to answer, so a failure is only logged."""

    async def run(_channel: Channel) -> None:
        try:
            await work
        except Exception:  # custom code: never let it crash the server
            log.exception("mcp_notification_handler_failed")

    return run


def _legacy_session(request: Request) -> str | None:
    """The legacy SSE transport's session (its messages URL's ``session_id``)."""
    session = request.query_params.get("session_id")
    try:
        return uuid.UUID(hex=session).hex if session else None
    except ValueError:
        return None


def _declared(messages: Sequence[JsonValue]) -> JsonObject:
    """The client capabilities the ``initialize`` among ``messages`` declared."""
    for message in messages:
        if isinstance(message, dict) and message.get("method") == "initialize":
            params = message.get("params")
            declared = params.get("capabilities") if isinstance(params, dict) else None
            if isinstance(declared, dict):
                return declared
    return {}


def _sent_arguments(exchange: Exchange) -> str | None:
    """The ``arguments`` a recorded ``tools/call`` sent, as canonical JSON (``None``: none)."""
    body = exchange.request.body.parsed()
    params = body.get("params") if isinstance(body, dict) else None
    if not isinstance(params, dict) or "arguments" not in params:
        return None
    return jsonfmt.canonical_dumps(params["arguments"])


def _messages(raw: bytes) -> list[JsonValue] | Response:
    """The JSON-RPC messages of a POST body, or the reply to one that doesn't parse."""
    try:
        parsed = jsonfmt.loads(raw)
    except ValueError:
        parse_error = protocol.error(None, protocol.PARSE_ERROR, "Parse error")
        return Response(
            content=jsonfmt.dumps(parse_error).encode(),
            status_code=400,
            media_type="application/json",
        )
    return parsed if isinstance(parsed, list) else [parsed]


def _drain(queue: asyncio.Queue[JsonValue]) -> list[JsonValue]:
    """Whatever is waiting in ``queue``, without waiting."""
    found = []
    while not queue.empty():
        found.append(queue.get_nowait())
    return found
