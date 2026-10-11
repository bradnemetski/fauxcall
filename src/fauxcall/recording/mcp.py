"""Record a remote MCP server: introspection, transport probes and tool calls.

Everything is captured at the wire level so the fake can reproduce the server exactly — including
how it negotiates protocol versions, whether it answers with JSON or SSE, how it treats missing
session ids, and the precise shape of its errors.

Every request counts against the budget and is paced; sessions are closed (``DELETE``) once, even
when the budget runs out. Exchanges get the same privacy treatment as REST recordings, including
the JSON-RPC payloads inside SSE events, and the configured credentials never reach disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx2

from fauxcall import jsonpath
from fauxcall.errors import RecordingError
from fauxcall.logs import get_logger
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient, RpcCall, Transport
from fauxcall.model.mcp import McpModel, McpToolModel, McpTransportModel
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange, StreamEvent
from fauxcall.recording.recorder import (
    CallOutcome,
    NoPrivacy,
    PrivacyFilter,
    RunReport,
    credential_values,
    private_text,
    scrub_exchange,
)
from fauxcall.runtime.mcp_server import method_endpoint, tool_endpoint
from fauxcall.runtime.templating import from_mapping, render
from fauxcall.storage import write_model
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from fauxcall.model.common import Purpose
    from fauxcall.model.plan import Approvals, PlannedCall, RecordingPlan
    from fauxcall.recording.store import RecordingStore
    from fauxcall.types import JsonObject, JsonValue

log = get_logger(__name__)

PROBE_TOOL: Final = "__fauxcall_probe_unknown_tool__"
PROBE_METHOD: Final = "fauxcall/probe-unknown-method"
MISSING_SESSION_PROBE_ID: Final = 9_000_001
_HTTP_OK: Final = 200
_HTTP_NOT_ALLOWED: Final = 405
_HTTP_ERROR: Final = 400
_TARGET_PARAM: Final = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
    "resources/subscribe": "uri",
    "resources/unsubscribe": "uri",
}
"""Methods whose endpoint id names their target (``MCP tools/call:<name>``): the param it fills."""


class _BudgetExhaustedError(RecordingError):
    """The recording budget ran out."""


def tool_is_mutating(definition: JsonValue) -> bool:
    """Per the MCP spec, a tool may modify state unless annotated ``readOnlyHint: true``."""
    if not isinstance(definition, dict):
        return True
    annotations = definition.get("annotations")
    if not isinstance(annotations, dict):
        return True
    return annotations.get("readOnlyHint") is not True


def _is_event_stream(content_type: str | None) -> bool:
    return bool(content_type) and "text/event-stream" in str(content_type).lower()


@dataclass(slots=True)
class McpIntrospection:
    """What introspection learned."""

    model: McpModel
    exchanges: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class McpRecorder:
    """Records one MCP server."""

    def __init__(  # noqa: PLR0913 - mirrors the CLI's recording options, plus injectable clocks
        self,
        store: RecordingStore,
        url: str,
        http: httpx2.AsyncClient,
        *,
        headers: Mapping[str, str] | None = None,
        privacy: PrivacyFilter | None = None,
        approvals: Approvals | None = None,
        approvals_path: Path | None = None,
        rps: float = 2.0,
        max_calls: int = 2000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.url = url
        self.http = http
        self.headers = dict(headers or {})
        self.privacy: PrivacyFilter = privacy or NoPrivacy()
        self.approvals = approvals
        self.approvals_path = approvals_path
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self.max_calls = max_calls
        self._sleep = sleep
        self._monotonic = monotonic
        self._secrets = credential_values(self.headers)
        self.calls_made = 0
        self._last: float | None = None

    # -- plumbing ----------------------------------------------------------------------------------

    async def _pace(self, *, cleanup: bool = False) -> None:
        """Wait for the rate limit and count one call (``cleanup`` calls are never refused)."""
        if not cleanup and self.calls_made >= self.max_calls:
            raise _BudgetExhaustedError("recording budget exhausted")
        if self._last is not None:
            wait = self._last + self.min_interval - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
        self._last = self._monotonic()
        self.calls_made += 1

    def _client(
        self, transport: Transport = "streamable_http", version: str | None = None
    ) -> McpWireClient:
        return McpWireClient(
            self.url,
            self.http,
            headers=self.headers,
            transport=transport,
            protocol_version=version or protocol.LATEST_HANDSHAKE_VERSION,
        )

    async def _close(self, client: McpWireClient) -> int | None:
        """End the client's session (one paced ``DELETE``) and close it; returns the status."""
        status = None
        if client.session_id and client.transport == "streamable_http":
            await self._pace(cleanup=True)
            with contextlib.suppress(httpx2.HTTPError):
                status = await client.delete_session()
        await client.close()
        return status

    def save(
        self,
        call: RpcCall,
        endpoint: str,
        purpose: Purpose = "discovery",
        plan_call: str | None = None,
        *,
        tags: Sequence[str] = (),
    ) -> str:
        """Store one wire exchange as an :class:`Exchange`; returns its id."""
        parts = urlsplit(self.url)
        redacted: list[str] = []
        pseudonymized: list[str] = []
        body = Body(kind="empty", content_type=call.content_type)
        if call.response is not None:  # the reply first: personal values it reveals become known
            value, red, pseudo = self.privacy.json(call.response)
            body = Body(kind="json", content_type=call.content_type, json_value=value)
            redacted += [f"response:{p}" for p in red]
            pseudonymized += [f"response:{p}" for p in pseudo]
        elif call.body:
            text = private_text(self.privacy, call.body, "response", redacted, pseudonymized)
            body = Body(kind="text", content_type=call.content_type, text=text)
        events = [
            StreamEvent(
                t_ms=round(t, 3),
                kind="sse",
                event=name,
                id=event_id,
                data=private_text(self.privacy, data, f"stream[{i}]", redacted, pseudonymized),
            )
            for i, (t, name, data, event_id) in enumerate(call.sse_events)
        ]
        url, safe_path, _, query, headers, sent, notes = self.privacy.request(
            url=self.url,
            path=parts.path or "/",
            path_params={},
            query=parse_qsl(parts.query, keep_blank_values=True),
            headers=call.request_headers,
            json_body=call.sent,
        )
        response_headers, header_notes = self.privacy.response_headers(call.response_headers)
        exchange = Exchange(
            id=self.store.next_id(),
            endpoint=endpoint,
            purpose=purpose,
            plan_call=plan_call,
            started_at=datetime.fromtimestamp(call.started_ns / 1e9, UTC),
            started_ns=call.started_ns,
            elapsed_ms=round(call.elapsed_ms, 3),
            request=CapturedRequest(
                method=HttpMethod.POST,
                url=url,
                path=safe_path,
                query=query,
                headers=headers,
                body=Body(kind="json", content_type="application/json", json_value=sent),
            ),
            response=CapturedResponse(status=call.status, headers=response_headers, body=body),
            stream=events,
            tags=list(tags),
            redacted=[*notes, *redacted, *(f"response.header:{n}" for n in header_notes)],
            pseudonymized=pseudonymized,
        )
        exchange = scrub_exchange(exchange, self._secrets)
        self.store.save(exchange)
        return exchange.id

    # -- introspection -----------------------------------------------------------------------------

    async def _connect(self) -> tuple[McpWireClient, RpcCall]:
        """Initialize over Streamable HTTP, falling back to the legacy HTTP+SSE transport."""
        client = self._client()
        await self._pace()
        try:
            return client, await client.initialize()
        except (RecordingError, httpx2.HTTPError) as first:
            log.info("mcp_streamable_failed_trying_sse", error=str(first)[:200])
            await self._close(client)
        client = self._client("sse")
        await self._pace()
        try:
            return client, await client.initialize()
        except (RecordingError, httpx2.HTTPError, TimeoutError) as second:
            await self._close(client)
            raise RecordingError(
                f"{self.url} does not speak MCP over Streamable HTTP or SSE: {second}"
            ) from second

    async def introspect(self, *, probe_versions: bool = True) -> McpIntrospection:
        """Capture the server's identity, capabilities, listings and transport behavior."""
        client, init = await self._connect()
        exchanges = [self.save(init, method_endpoint("initialize"))]
        result = init.result if isinstance(init.result, dict) else {}
        capabilities = result.get("capabilities")
        model = McpModel(initialize_result=result)
        try:
            await self._listings(
                client, capabilities if isinstance(capabilities, dict) else {}, model, exchanges
            )
            model.transport, missing_session = await self._probe_transport(client, init, exchanges)
            model.errors.update(await self._probe_errors(client, exchanges))
            if missing_session is not None:
                model.errors["missing_session"] = missing_session
        finally:
            status = await self._close(client)
        if status is not None:
            model.transport.delete_status = status
        if probe_versions and model.transport.kind == "streamable_http":
            model.transport.protocol_versions = await self._probe_versions(exchanges)
        counts = (len(model.tools), len(model.resources), len(model.prompts))
        notes = ["{} tools, {} resources, {} prompts".format(*counts)]
        return McpIntrospection(model=model, exchanges=exchanges, notes=notes)

    async def _listings(
        self,
        client: McpWireClient,
        capabilities: JsonObject,
        model: McpModel,
        exchanges: list[str],
    ) -> None:
        """Every page of every advertised listing (tools, resources, templates, prompts)."""
        listings = (
            ("tools", "tools/list", "tools"),
            ("resources", "resources/list", "resources"),
            ("resources", "resources/templates/list", "resourceTemplates"),
            ("prompts", "prompts/list", "prompts"),
        )
        for capability, method, key in listings:
            if capability not in capabilities:
                continue
            items, calls = await client.list_all(method, key, before_page=self._pace)
            exchanges += [self.save(c, method_endpoint(method)) for c in calls]
            if method == "tools/list":
                model.tools = [McpToolModel(definition=item) for item in items]
                first = calls[0].result if calls else None
                page = first.get("tools") if isinstance(first, dict) else None
                if isinstance(page, list) and page and len(calls) > 1:
                    model.tools_page_size = len(page)
            elif method == "resources/list":
                model.resources = items
            elif method == "resources/templates/list":
                model.resource_templates = items
            else:
                model.prompts = items

    async def _probe_transport(
        self, client: McpWireClient, init: RpcCall, exchanges: list[str]
    ) -> tuple[McpTransportModel, JsonValue]:
        """How the server speaks the transport; returns it and the missing-session error body."""
        legacy = client.transport == "sse"
        content_type = client.sse_content_type if legacy else init.content_type
        response_mode: Literal["json", "sse"] = (
            "sse" if legacy or _is_event_stream(content_type) else "json"
        )
        messages_url = client.messages_url
        transport = McpTransportModel(
            kind=client.transport,
            path=urlsplit(self.url).path or "/mcp",
            messages_path=urlsplit(messages_url).path if legacy and messages_url else None,
            response_mode=response_mode,
            issues_session=client.session_id is not None,
            default_protocol_version=client.negotiated_version or protocol.LATEST_HANDSHAKE_VERSION,
            sse_event_ids=any(event_id is not None for *_, event_id in init.sse_events),
        )
        if content_type:
            if response_mode == "sse":
                transport.content_type_sse = content_type
            else:
                transport.content_type_json = content_type
        if legacy:
            return transport, None
        with contextlib.suppress(httpx2.HTTPError):
            await self._pace()
            status, stream_type = await client.get_stream_status()
            if status == _HTTP_OK and _is_event_stream(stream_type):
                transport.get_stream = "sse"
            else:
                transport.get_stream = "status"
                transport.get_status = status if status >= _HTTP_ERROR else _HTTP_NOT_ALLOWED
        missing_session: JsonValue = None
        if client.session_id:
            await self._pace()
            probe = protocol.request(MISSING_SESSION_PROBE_ID, "tools/list")
            missing = await client.post(probe, include_session=False)
            exchanges.append(self.save(missing, method_endpoint("tools/list", "missing-session")))
            transport.missing_session_status = missing.status
            transport.requires_session = missing.status >= _HTTP_ERROR
            if transport.requires_session and isinstance(missing.response, dict):
                missing_session = missing.response
        return transport, missing_session

    async def _probe_errors(
        self, client: McpWireClient, exchanges: list[str]
    ) -> dict[str, JsonValue]:
        """The server's exact errors for an unknown method and an unknown tool."""
        errors: dict[str, JsonValue] = {}
        await self._pace()
        unknown_method = await client.call(PROBE_METHOD)
        exchanges.append(self.save(unknown_method, method_endpoint(PROBE_METHOD)))
        if isinstance(unknown_method.response, dict):
            errors["method_not_found"] = unknown_method.response
        await self._pace()
        unknown_tool = await client.call("tools/call", {"name": PROBE_TOOL, "arguments": {}})
        exchanges.append(self.save(unknown_tool, tool_endpoint(PROBE_TOOL)))
        if isinstance(unknown_tool.response, dict):
            errors["unknown_tool"] = unknown_tool.response
        return errors

    async def _probe_versions(self, exchanges: list[str]) -> dict[str, str]:
        """Which protocol version the server answers to each known version it is offered."""
        mapping: dict[str, str] = {}
        for version in protocol.KNOWN_VERSIONS:
            client = self._client(version=version)
            try:
                await self._pace()
                call = await client.initialize(protocol_version=version)
            except _BudgetExhaustedError:
                break
            except (RecordingError, httpx2.HTTPError, TimeoutError) as exc:
                log.info("mcp_version_probe_failed", version=version, error=str(exc)[:200])
                continue
            else:
                exchanges.append(
                    self.save(call, method_endpoint("initialize", f"version-{version}"))
                )
                if client.negotiated_version:
                    mapping[version] = client.negotiated_version
            finally:
                await self._close(client)
        return mapping

    # -- tool calls --------------------------------------------------------------------------------

    async def run(
        self, plan: RecordingPlan, tools: Mapping[str, JsonValue] | None = None
    ) -> RunReport:
        """Execute MCP calls of a plan inside one session.

        ``request.json_body`` holds the JSON-RPC params; the endpoint id names the method
        (``MCP tools/call:<tool>``, ``MCP resources/read:<uri>``, ``MCP prompts/get:<name>`` …) and
        fills in the params' ``name``/``uri`` when the plan leaves it out. Params may use
        ``{{capture}}`` placeholders, and ``capture`` paths apply to the whole JSON-RPC response
        (``$.result.structuredContent.id``). Tool calls need an approval unless the tool is
        annotated ``readOnlyHint: true`` in ``tools`` (or the call sets ``mutating: false``).
        """
        report = RunReport(plan=plan.name)
        definitions = tools or {}
        variables: dict[str, JsonValue] = {}
        client, init = await self._connect()
        try:
            self.save(init, method_endpoint("initialize"), purpose="sample")
            for position, call in enumerate(plan.calls):
                outcome = CallOutcome(call=call.id, endpoint=call.endpoint)
                report.outcomes.append(outcome)
                needs_approval = self._needs_approval(call, definitions)
                if needs_approval and (self.approvals is None or self.approvals.find(call) is None):
                    outcome.skipped = "needs approval (tool may modify state)"
                    continue
                try:
                    await self._run_call(
                        client, call, variables, outcome, needs_approval=needs_approval
                    )
                except _BudgetExhaustedError:
                    outcome.skipped = "budget exhausted"
                    report.budget_exhausted = True
                    report.outcomes += [
                        CallOutcome(
                            call=rest.id, endpoint=rest.endpoint, skipped="budget exhausted"
                        )
                        for rest in plan.calls[position + 1 :]
                    ]
                    break
                except (RecordingError, httpx2.HTTPError, TimeoutError, ValueError) as exc:
                    outcome.error = f"{type(exc).__name__}: {exc}"[:300]
                    log.warning("mcp_call_failed", call=call.id, error=outcome.error)
        finally:
            await self._close(client)
            report.calls_made = self.calls_made
            if self.approvals is not None and self.approvals_path is not None:
                write_model(self.approvals_path, self.approvals)
        return report

    async def _run_call(
        self,
        client: McpWireClient,
        call: PlannedCall,
        variables: dict[str, JsonValue],
        outcome: CallOutcome,
        *,
        needs_approval: bool,
    ) -> None:
        method = _method_of(call.endpoint)
        for index in range(call.repeat):
            if index and call.interval_ms:
                await self._sleep(call.interval_ms / 1000)
            approval = None
            if needs_approval:
                approval = self.approvals.find(call) if self.approvals is not None else None
                if approval is None:
                    outcome.skipped = f"approval budget used up after {index} call(s)"
                    return
            await self._pace()  # raises before an approval is spent when the budget is gone
            if approval is not None and self.approvals is not None:
                self.approvals.consume(approval)
            params = render(call.request.json_body, from_mapping(variables))
            rpc = await client.call(method, _params_for(call.endpoint, params))
            outcome.exchanges.append(
                self.save(rpc, call.endpoint, call.purpose, call.id, tags=call.tags)
            )
            outcome.statuses.append(rpc.status)
            for name, path in call.capture.items():
                captured = jsonpath.get_one(rpc.response, path)
                if captured is not None:
                    variables[name] = captured

    @staticmethod
    def _needs_approval(call: PlannedCall, tools: Mapping[str, JsonValue]) -> bool:
        if call.mutating is not None:
            return call.mutating
        if call.endpoint.startswith("MCP tools/call:"):
            return tool_is_mutating(tools.get(call.endpoint.split(":", 1)[1]))
        return False


def _method_of(endpoint: str) -> str:
    """``"MCP tools/call:ask"`` -> ``"tools/call"``."""
    body = endpoint.removeprefix("MCP ").strip()
    return body.split(":", 1)[0]


def _params_for(endpoint: str, params: JsonValue) -> JsonValue:
    """The JSON-RPC params, with the target the endpoint id names filled in when missing.

    ``("MCP tools/call:ask", {"arguments": {…}})`` -> ``{"name": "ask", "arguments": {…}}``.
    """
    method, _, detail = endpoint.removeprefix("MCP ").strip().partition(":")
    key = _TARGET_PARAM.get(method)
    if key is None or not detail:
        return params
    if params is None:
        return {key: detail}
    if isinstance(params, dict) and key not in params:
        return {key: detail, **params}
    return params
