"""The fake MCP server: Streamable HTTP and legacy HTTP+SSE, reproducing the real wire behavior.

Tool definitions, capabilities, server info and instructions are served verbatim from the model;
tool calls are answered from recordings (exact arguments first, then the closest recording —
Jev when available), by custom handlers, or with the real server's error shapes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
import time
import uuid
from typing import TYPE_CHECKING, Final

import jsonschema
from starlette.responses import Response, StreamingResponse

from fauxcall import jsonfmt
from fauxcall.clock import ns_to_datetime
from fauxcall.logs import bind_context, clear_context, get_logger
from fauxcall.mcpwire import protocol
from fauxcall.model.mcp import FALLBACK_PROTOCOL_VERSION
from fauxcall.runtime.inputs import json_inputs
from fauxcall.runtime.journal import JournalEntry
from fauxcall.runtime.matching import Candidate, match

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.requests import Request

    from fauxcall.model.mcp import McpModel, McpToolModel
    from fauxcall.runtime.app import FakeRuntime
    from fauxcall.runtime.context import RequestContext
    from fauxcall.types import JsonObject, JsonValue

    ToolHandler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]

log = get_logger(__name__)

TOOLS_CALL: Final = "tools/call"
KEEP_ALIVE_SECONDS: Final = 15.0


def _is_error_answer(answer: JsonObject) -> bool:
    """A JSON-RPC error, or a tool result that reports one (``isError``)."""
    result = answer.get("result")
    return "error" in answer or (isinstance(result, dict) and result.get("isError") is True)


class _InvalidCursorError(ValueError):
    """A ``cursor`` this server never issued."""


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
        self._sessions: dict[str, str] = {}
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

    def _error(self, request_id: JsonValue, situation: str, code: int, message: str) -> JsonObject:
        recorded = self.model.errors.get(situation)
        if isinstance(recorded, dict):
            return protocol.with_id(recorded, request_id)
        return protocol.error(request_id, code, message)

    async def _recorded_result(
        self,
        endpoint_id: str,
        inputs: dict[str, JsonValue],
        tool: McpToolModel | None,
    ) -> JsonValue | None:
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
            candidates.append(Candidate(exchange.id, flatten_args(arguments)))
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
            case "ping" | "logging/setLevel":
                return protocol.result(request_id, {})
            case "tools/list":
                return protocol.result(
                    request_id,
                    self._page(params, [t.definition for t in self.model.tools], "tools"),
                )
            case "resources/list":
                return protocol.result(
                    request_id, self._page(params, self.model.resources, "resources")
                )
            case "resources/templates/list":
                return protocol.result(
                    request_id,
                    self._page(params, self.model.resource_templates, "resourceTemplates"),
                )
            case "prompts/list":
                return protocol.result(
                    request_id, self._page(params, self.model.prompts, "prompts")
                )
            case "tools/call":
                return await self._call_tool(ctx, request_id, params)
            case "resources/read" | "prompts/get" | "completion/complete":
                detail = params.get("uri") or params.get("name")
                endpoint_id = method_endpoint(method, str(detail) if detail else None)
                inputs = json_inputs("params", params)
                recorded = await self._recorded_result(endpoint_id, inputs, None)
                if isinstance(recorded, dict):
                    return protocol.with_id(recorded, request_id)
                return self._error(
                    request_id, f"{method}_not_found", protocol.INVALID_PARAMS, "Not found"
                )
            case _:
                return self._error(
                    request_id, "method_not_found", protocol.METHOD_NOT_FOUND, "Method not found"
                )

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
        name = str(params.get("name", ""))
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        assert isinstance(arguments, dict)  # noqa: S101
        tool = self.model.tool(name)
        if tool is None:
            return self._error(
                request_id, "unknown_tool", protocol.INVALID_PARAMS, f"Unknown tool: {name}"
            )
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
        answer = await self._tool_answer(ctx, request_id, tool, arguments)
        await self._tool_latency(ctx, tool, answer, started)
        return answer

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
            value = await handler(ctx, arguments)
            return protocol.result(request_id, value)
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

    def _reply(self, messages: list[JsonObject], headers: dict[str, str]) -> Response:
        if self.transport.response_mode == "sse":
            text = "".join(
                protocol.sse_event(
                    m, event_id=secrets.token_hex(8) if self.transport.sse_event_ids else None
                )
                for m in messages
            )
            return Response(
                content=text.encode(), media_type=self.transport.content_type_sse, headers=headers
            )
        payload: JsonValue = messages[0] if len(messages) == 1 else list(messages)
        return Response(
            content=jsonfmt.dumps(payload).encode(),
            media_type=self.transport.content_type_json,
            headers=headers,
        )

    async def streamable(self, request: Request) -> Response:
        """Handle GET/POST/DELETE on the Streamable HTTP endpoint."""
        session_header = self.transport.session_header
        session = request.headers.get(session_header)
        if request.method == "GET":
            if self.transport.get_stream == "status":
                status = self.transport.get_status
                allow = {"allow": "POST, DELETE"} if status == 405 else {}  # noqa: PLR2004
                return Response(status_code=status, headers=allow)
            return StreamingResponse(
                _idle_stream(self.runtime.pause), media_type=self.transport.content_type_sse
            )
        if request.method == "DELETE":
            if session:
                self._sessions.pop(session, None)
            return Response(status_code=self.transport.delete_status)
        raw = await request.body()
        try:
            parsed = jsonfmt.loads(raw)
        except ValueError:
            parse_error = protocol.error(None, protocol.PARSE_ERROR, "Parse error")
            return Response(
                content=jsonfmt.dumps(parse_error).encode(),
                status_code=400,
                media_type="application/json",
            )
        messages = parsed if isinstance(parsed, list) else [parsed]
        is_init = any(isinstance(m, dict) and m.get("method") == "initialize" for m in messages)
        headers: dict[str, str] = {}
        if is_init and self.transport.issues_session:
            session = secrets.token_hex(16)
            self._sessions[session] = (
                request.headers.get(self.runtime.settings.session_header) or "default"
            )
            headers[session_header] = session
        elif (
            self.transport.requires_session
            and self.transport.issues_session
            and (not session or session not in self._sessions)
        ):
            missing: JsonValue = self.model.errors.get("missing_session") or protocol.error(
                None, protocol.INVALID_REQUEST, "Bad Request: No valid session ID provided"
            )
            return Response(
                content=jsonfmt.dumps(missing).encode(),
                status_code=self.transport.missing_session_status,
                media_type="application/json",
            )
        requests = [m for m in messages if protocol.is_request(m)]
        if not requests:
            return Response(status_code=202)
        replies = [await self._answer(request, m) for m in requests if isinstance(m, dict)]
        return self._reply(replies, headers)

    async def _answer(self, request: Request, message: JsonObject) -> JsonObject:
        started = time.perf_counter()
        method = str(message.get("method"))
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        endpoint_id = tool_endpoint(str(name)) if method == TOOLS_CALL else method_endpoint(method)
        ctx = self.runtime.new_context(request, None)
        ctx.identity = getattr(request.state, "fauxcall_identity", None)
        bind_context(request_id=ctx.request_id, session=ctx.session, endpoint=endpoint_id)
        fault = self.runtime.faults.pick(endpoint_id, ctx.session)
        if fault is not None and fault.kind != "delay":
            reply = protocol.error(
                message.get("id"), protocol.INTERNAL_ERROR, f"fauxcall injected {fault.kind}"
            )
        else:
            if fault is not None:
                await self.runtime.pause(fault.delay_ms / 1000)
            reply = await self.respond_to(ctx, message)
        self._journal(
            ctx, request, endpoint_id, message, reply, started, fault.kind if fault else None
        )
        clear_context()
        return reply

    def _journal(
        self,
        ctx: RequestContext,
        request: Request,
        endpoint_id: str,
        message: JsonObject,
        reply: JsonObject,
        started: float,
        fault: str | None,
    ) -> None:
        if not self.runtime.journal.enabled:
            return
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
                status=200,
                response_body=reply,
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
            request.headers.get(self.runtime.settings.session_header) or "default"
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
        for message in parsed if isinstance(parsed, list) else [parsed]:
            if protocol.is_request(message) and isinstance(message, dict):
                reply = await self._answer(request, message)
                await queue.put(protocol.sse_event(reply))
        return Response(content=b"Accepted", status_code=202)


async def _idle_stream(pause: Callable[[float], Awaitable[None]]) -> AsyncIterator[bytes]:
    """The server-to-client stream of a server that never pushes: comments keep it open."""
    while True:
        await pause(KEEP_ALIVE_SECONDS)
        yield b": keep-alive\n\n"
