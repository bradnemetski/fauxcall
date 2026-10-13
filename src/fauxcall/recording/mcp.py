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
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx2

from fauxcall import jsonfmt, jsonpath
from fauxcall.errors import CredentialsRefusedError, HttpStatusError, RecordingError
from fauxcall.logs import get_logger
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient, RpcCall, Transport
from fauxcall.model.auth import AuthModel, AuthScheme, OAuthModel
from fauxcall.model.errors import ErrorTemplate
from fauxcall.model.mcp import (
    PROBE_PROMPT,
    PROBE_RESOURCE,
    PROBE_TOOL,
    McpModel,
    McpToolModel,
    McpTransportModel,
)
from fauxcall.model.plan import Approval, Approvals
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange, StreamEvent
from fauxcall.recording.recorder import (
    INVALID_CREDENTIAL,
    NOT_SENT,
    CallOutcome,
    NoPrivacy,
    PrivacyFilter,
    RunReport,
    credential_values,
    keep_scheme,
    private_text,
    scrub_exchange,
)
from fauxcall.runtime.mcp_server import method_endpoint, tool_endpoint
from fauxcall.runtime.templating import from_mapping, render
from fauxcall.storage import read_model, write_model
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from fauxcall.model.common import Purpose
    from fauxcall.model.plan import PlannedCall, RecordingPlan
    from fauxcall.recording.store import RecordingStore
    from fauxcall.types import JsonObject, JsonValue

log = get_logger(__name__)

PROBE_METHOD: Final = "fauxcall/probe-unknown-method"
CLIENT_CAPABILITIES: Final[dict[str, JsonObject]] = {
    "sampling": {"sampling": {}},
    "elicitation": {"elicitation": {}},
    "elicitation.url": {"elicitation": {"form": {}, "url": {}}},
    "roots": {"roots": {"listChanged": True}},
    "tasks.requests": {
        "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}}
    },
}
"""Client capabilities introspection declares one at a time (by the path ``requires`` names), to
find the tools they unlock; then all of them together."""
_ALL: Final = "all"


def _all_capabilities() -> JsonObject:
    """Every probed capability at once (nested objects merged)."""
    merged: JsonObject = {}
    for declared in CLIENT_CAPABILITIES.values():
        _merge(merged, declared)
    return merged


def _merge(into: JsonObject, extra: JsonObject) -> None:
    for key, value in extra.items():
        current = into.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value)
        else:
            into[key] = dict(value) if isinstance(value, dict) else value


MISSING_SESSION_PROBE_ID: Final = 9_000_001
AUTH_PROBE_ID: Final = 9_000_002
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


def needs_approval(call: PlannedCall, tools: Mapping[str, JsonValue]) -> bool:
    """Whether a planned call is a write, which needs the user's up-front approval.

    An explicit ``mutating`` wins. MCP: only ``tools/call`` of a tool that may modify state (no
    ``readOnlyHint: true`` in its definition in ``tools``); a name missing from a known listing is
    a probe the server refuses as unknown, and the protocol's own methods (``initialize``,
    listings, reads) never write. HTTP: anything but GET/HEAD/OPTIONS.
    """
    if call.mutating is not None:
        return call.mutating
    if call.endpoint.startswith("MCP tools/call:"):
        name = call.endpoint.split(":", 1)[1]
        if tools and name not in tools:  # a name the server doesn't list: refused as unknown
            return False
        return tool_is_mutating(tools.get(name))
    if call.endpoint.startswith("MCP "):
        return False
    return call.is_mutating


def _is_event_stream(content_type: str | None) -> bool:
    return bool(content_type) and "text/event-stream" in str(content_type).lower()


_PASSING_STATUSES: Final = frozenset({405, 408, 425, 429})
_REFUSED: Final = frozenset({401, 403})


_RESOURCE_METADATA: Final = re.compile(r'resource_metadata="([^"]+)"')


def _static_scheme(headers: Mapping[str, str]) -> AuthScheme:
    """How the configured credentials are sent: a bearer token, or a key in a header."""
    for name, value in headers.items():
        if name.lower() == "authorization":
            scheme = value.strip().partition(" ")[0].lower()
            return AuthScheme(kind="basic" if scheme == "basic" else "bearer")
    name = next(iter(headers))
    return AuthScheme(kind="api_key", location="header", name=name)


def _may_pass(error: Exception) -> bool:
    """Whether trying again could succeed: a dropped connection, a timeout or a server hiccup."""
    if isinstance(error, HttpStatusError):
        return error.status >= 500 or error.status in _PASSING_STATUSES  # noqa: PLR2004
    return isinstance(error, httpx2.HTTPError | TimeoutError)


def _describe(error: Exception) -> str:
    return str(error) or type(error).__name__


def _listing_extras(result: JsonValue, key: str) -> JsonObject | None:
    """A listing result's members in order, the list itself as ``None``, when it has more.

    ``None`` when the result is just the list (and ``nextCursor``): nothing to reproduce.
    """
    if not isinstance(result, dict) or key not in result:
        return None
    if not set(result) - {key, "nextCursor"}:
        return None
    return {
        name: None if name == key else value
        for name, value in result.items()
        if name != "nextCursor"
    }


@dataclass(slots=True)
class McpIntrospection:
    """What introspection learned."""

    model: McpModel
    exchanges: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    auth: AuthModel | None = None
    """How the server authenticates, learned by calling it without the credentials."""
    auth_error: ErrorTemplate | None = None
    """Its answer to a call without credentials (the ``unauthorized`` template)."""


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
        """Initialize over Streamable HTTP, falling back to the legacy HTTP+SSE transport.

        A Streamable HTTP failure that may be passing (a 5xx, 405 or 429, a dropped connection) is
        tried once more first. A refusal of the credentials says so instead of blaming the
        transport, and every failure names what each transport answered.
        """
        failures: list[Exception] = []
        attempts: tuple[tuple[Transport, int], ...] = (("streamable_http", 2), ("sse", 1))
        for transport, tries in attempts:
            for _ in range(tries):
                client = self._client(transport)
                await self._pace()
                try:
                    return client, await client.initialize()
                except (RecordingError, httpx2.HTTPError, TimeoutError) as error:
                    log.info("mcp_connect_failed", transport=transport, error=str(error)[:200])
                    await self._close(client)
                    if isinstance(error, HttpStatusError) and error.status in _REFUSED:
                        # It speaks this transport; the other one won't take the key either.
                        raise CredentialsRefusedError(self._auth_problem(error)) from error
                    failures.append(error)
                    if not _may_pass(error):
                        break
        streamable, sse = failures[0], failures[-1]
        raise RecordingError(
            f"{self.url} does not speak MCP over Streamable HTTP or SSE. "
            f"Streamable HTTP: {_describe(streamable)}; SSE: {_describe(sse)}"
        ) from sse

    def _auth_problem(self, error: HttpStatusError) -> str:
        """Why the server refused, and what to fix (never showing a credential)."""
        challenge = (
            f" (WWW-Authenticate: {error.www_authenticate})" if error.www_authenticate else ""
        )
        message = f"{self.url} refused the credentials: HTTP {error.status}{challenge}."
        authorization = next(
            (v for k, v in self.headers.items() if k.lower() == "authorization"), None
        )
        if not self.headers:
            message += (
                " No credentials were sent: put them in the fake's .env as"
                ' FAUXCALL_REAL__HEADERS=\'{"Authorization": "Bearer <token>"}\''
                " (or pass --auth-env NAME=ENV_VAR), then run init again."
            )
        elif authorization is not None and " " not in authorization.strip():
            message += (
                " The Authorization value has no scheme; most servers expect"
                ' "Bearer <token>". Check FAUXCALL_REAL__HEADERS in the fake\'s .env.'
            )
        else:
            message += (
                " Check that the credentials in FAUXCALL_REAL__HEADERS (the fake's .env) are"
                " current and allowed to use this server."
            )
        return message

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
            model.errors.update(
                await self._probe_errors(
                    client, capabilities if isinstance(capabilities, dict) else {}, exchanges
                )
            )
            with contextlib.suppress(_BudgetExhaustedError):  # optional, budget allowing
                await self._probe_malformed(client, model, exchanges)
            if missing_session is not None:
                model.errors["missing_session"] = missing_session
            if model.transport.kind == "streamable_http":  # optional: last, budget allowing
                await self._capability_tools(model, exchanges)
        finally:
            status = await self._close(client)
        if status is not None:
            model.transport.delete_status = status
        if probe_versions and model.transport.kind == "streamable_http":
            model.transport.protocol_versions = await self._probe_versions(exchanges)
        counts = (len(model.tools), len(model.resources), len(model.prompts))
        notes = ["{} tools, {} resources, {} prompts".format(*counts)]
        found = McpIntrospection(model=model, exchanges=exchanges, notes=notes)
        if self.headers and model.transport.kind == "streamable_http":
            await self._probe_auth(found)
        return found

    async def _probe_auth(self, found: McpIntrospection) -> None:
        """Call the server without the credentials: its refusal, and OAuth metadata if any.

        The fake then refuses the same way, and when the server publishes OAuth discovery
        documents it serves them too (with its own built-in authorization server), so OAuth
        clients can sign in to the fake as they do to the real server.
        """
        probe = McpWireClient(self.url, self.http, headers={})
        await self._pace()
        params: JsonObject = {
            "protocolVersion": protocol.LATEST_HANDSHAKE_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fauxcall", "version": "probe"},
        }
        message = protocol.request(AUTH_PROBE_ID, "initialize", params)
        call = await probe.post(message, include_session=False)
        exchange = self.save(
            call, method_endpoint("initialize", "no-credentials"), "auth", tags=["auth:none"]
        )
        found.exchanges.append(exchange)
        if call.status not in _REFUSED:
            found.notes.append(f"answers without credentials too (HTTP {call.status})")
            return
        challenge = call.header("www-authenticate")
        oauth = await self._oauth_metadata(challenge)
        schemes = [_static_scheme(self.headers)]
        if oauth is not None:
            schemes.append(AuthScheme(kind="oauth2", oauth=oauth))
        found.auth = AuthModel(
            schemes=schemes, missing_template="unauthorized", invalid_template="unauthorized"
        )
        reply = call.messages[0] if call.messages else None
        headers = {} if oauth is not None or not challenge else {"WWW-Authenticate": challenge}
        found.auth_error = ErrorTemplate(
            name="unauthorized",
            status=call.status,
            content_type=call.content_type,
            headers=headers,
            body_json=reply if reply is not None else None,
            body_text=None if reply is not None else (call.body or None),
            examples=[exchange],
        )
        kinds = " and ".join(scheme.kind for scheme in schemes)
        found.notes.append(f"refuses calls without credentials (HTTP {call.status}): {kinds}")

    async def _oauth_metadata(self, challenge: str | None) -> OAuthModel | None:
        """The protected-resource and authorization-server documents, when the server has them."""
        parts = urlsplit(self.url)
        root = f"{parts.scheme}://{parts.netloc}"
        named = _RESOURCE_METADATA.search(challenge or "")
        path = parts.path.rstrip("/")
        resource = await self._json(
            named[1] if named else f"{root}/.well-known/oauth-protected-resource{path}"
        )
        if resource is None:
            return None
        servers = resource.get("authorization_servers")
        issuer = str(servers[0]).rstrip("/") if isinstance(servers, list) and servers else root
        server = await self._json(f"{issuer}/.well-known/oauth-authorization-server")
        if server is None:
            server = await self._json(f"{issuer}/.well-known/openid-configuration")
        supported = resource.get("scopes_supported")
        scopes = [str(s) for s in supported] if isinstance(supported, list) else []
        return OAuthModel(
            protected_resource_metadata=resource,
            authorization_server_metadata=server,
            scopes=scopes,
        )

    async def _json(self, url: str) -> JsonObject | None:
        """A public JSON document (no credentials sent), or ``None``."""
        await self._pace()
        try:
            response = await self.http.get(url, headers={"accept": "application/json"})
        except httpx2.HTTPError:
            return None
        if response.status_code != _HTTP_OK:
            return None
        try:
            document = jsonfmt.loads(response.content)
        except ValueError:
            return None
        return document if isinstance(document, dict) else None

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
            extras = _listing_extras(calls[0].result if calls else None, key)
            if extras is not None:
                model.listing_extras[method] = extras
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

    async def _capability_tools(self, model: McpModel, exchanges: list[str]) -> None:
        """Tools the server lists only to clients declaring a capability (sampling …).

        One session per capability (:data:`CLIENT_CAPABILITIES`), then one declaring all of them;
        tools missing from the plain listing are added with ``requires``: each capability that
        unlocked them on its own, else all of them together.
        """
        known = {tool.name for tool in model.tools}
        unlocked: dict[str, tuple[JsonValue, list[str]]] = {}
        probes = [*CLIENT_CAPABILITIES.items(), (_ALL, _all_capabilities())]
        for label, declared in probes:
            listed = await self._tools_for(label, declared, exchanges)
            if listed is None:
                if self._spent():
                    break
                continue
            for definition in listed:
                name = definition.get("name") if isinstance(definition, dict) else None
                if isinstance(name, str) and name not in known:
                    unlocked.setdefault(name, (definition, []))[1].append(label)
        for definition, labels in unlocked.values():
            alone = [[label] for label in labels if label != _ALL]
            needs = alone or [list(CLIENT_CAPABILITIES)]
            model.tools.append(McpToolModel(definition=definition, requires=needs))

    def _spent(self) -> bool:
        return self.calls_made >= self.max_calls

    async def _tools_for(
        self, label: str, declared: JsonObject, exchanges: list[str]
    ) -> list[JsonValue] | None:
        """The tools listed to a client declaring ``declared`` (``None``: the probe failed)."""
        client = self._client()
        try:
            await self._pace()
            await client.initialize(capabilities=declared)
            tools, calls = await client.list_all("tools/list", "tools", before_page=self._pace)
        except _BudgetExhaustedError:
            return None  # the recording budget is spent: fewer probes, not a failed init
        except (RecordingError, httpx2.HTTPError, TimeoutError) as error:
            log.info("mcp_capability_probe_failed", capability=label, error=str(error))
            return None
        finally:
            await self._close(client)
        exchanges += [self.save(c, method_endpoint("tools/list", f"with-{label}")) for c in calls]
        return tools

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
            path=urlsplit(self.url).path or "/",
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
        self, client: McpWireClient, capabilities: JsonObject, exchanges: list[str]
    ) -> dict[str, JsonValue]:
        """The server's exact errors for an unknown method, tool, resource and prompt.

        The fake answers with them, putting the caller's name where they repeat the probe's.
        """
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
        probes = (
            ("resources", "resources/read", "uri", PROBE_RESOURCE),
            ("prompts", "prompts/get", "name", PROBE_PROMPT),
        )
        for capability, method, param, target in probes:
            if capability not in capabilities:
                continue
            await self._pace()
            unknown = await client.call(method, {param: target})
            exchanges.append(self.save(unknown, method_endpoint(method, target)))
            if isinstance(unknown.response, dict) and "error" in unknown.response:
                errors[f"{method}_not_found"] = unknown.response
        return errors

    async def _probe_malformed(
        self, client: McpWireClient, model: McpModel, exchanges: list[str]
    ) -> None:
        """The server's answers to malformed calls, which the fake repeats.

        A ``tools/call`` without a name; ``arguments`` that aren't an object (sent to a read-only
        tool, so nothing can change); and a second ``initialize`` in the same session (last: it
        may end the session).
        """
        await self._pace()
        nameless = await client.call("tools/call", {"arguments": {}})
        exchanges.append(self.save(nameless, tool_endpoint(""), "invalid"))
        if isinstance(nameless.response, dict) and "error" in nameless.response:
            model.errors["tool_call_without_name"] = nameless.response
        reader = next((t for t in model.tools if not tool_is_mutating(t.definition)), None)
        if reader is not None:
            await self._pace()
            odd = await client.call("tools/call", {"name": reader.name, "arguments": []})
            exchanges.append(self.save(odd, tool_endpoint(reader.name), "invalid"))
            if isinstance(odd.response, dict) and "error" in odd.response:
                model.errors["arguments_not_object"] = odd.response
                model.arguments_probe_tool = reader.name
        if client.session_id is None:
            return
        await self._pace()
        params: JsonObject = {
            "protocolVersion": client.negotiated_version or protocol.LATEST_HANDSHAKE_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fauxcall", "version": "probe"},
        }
        again = await client.call("initialize", params)
        exchanges.append(self.save(again, method_endpoint("initialize", "again"), "invalid"))
        if isinstance(again.response, dict) and "error" in again.response:
            model.errors["second_initialize"] = again.response
            model.transport.reinitialize_status = again.status

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
                writes = needs_approval(call, definitions)
                if writes and self._current_approvals().find(call) is None:
                    outcome.skipped = "needs approval (tool may modify state)"
                    continue
                try:
                    await self._run_call(client, call, variables, outcome, needs_approval=writes)
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
        return report

    async def _auth_probe(
        self, client: McpWireClient, call: PlannedCall, method: str, params: JsonValue
    ) -> RpcCall:
        """Send one request without credentials (``auth: none``) or with a bad one (``invalid``).

        It goes outside the session, to record how the real server refuses it.
        """
        if client.transport != "streamable_http":
            raise RecordingError("auth probes need the Streamable HTTP transport")
        headers = (
            {}
            if call.request.auth == "none"
            else {
                name: keep_scheme(value, INVALID_CREDENTIAL, header=name)
                for name, value in self.headers.items()
            }
        )
        probe = McpWireClient(
            self.url,
            self.http,
            headers=headers,
            protocol_version=client.negotiated_version or protocol.LATEST_HANDSHAKE_VERSION,
        )
        request_id = AUTH_PROBE_ID
        message = protocol.request(request_id, method, _params_for(call.endpoint, params))
        return await probe.post(message, include_session=False)

    def _current_approvals(self) -> Approvals:
        """The approvals in force: the file when there is one (it may have changed meanwhile)."""
        if self.approvals_path is not None and self.approvals_path.exists():
            self.approvals = read_model(self.approvals_path, Approvals)
        return self.approvals if self.approvals is not None else Approvals()

    def _spend(self, call: PlannedCall) -> Approval | None:
        """Spend one call of the approval covering ``call`` and save it at once (``None``: none)."""
        approvals = self._current_approvals()
        approval = approvals.find(call)
        if approval is None:
            return None
        approvals.consume(approval)
        self.approvals = approvals
        if self.approvals_path is not None:
            write_model(self.approvals_path, approvals)
        return approval

    def _refund(self, approval: Approval | None) -> None:
        """Give back a spent call whose request never reached the server."""
        if approval is None:
            return
        approvals = self._current_approvals()
        approvals.refund(approval)
        self.approvals = approvals
        if self.approvals_path is not None:
            write_model(self.approvals_path, approvals)

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
            if needs_approval and self._current_approvals().find(call) is None:
                outcome.skipped = f"approval budget used up after {index} call(s)"
                return
            await self._pace()  # raises before an approval is spent when the budget is gone
            approval = self._spend(call) if needs_approval else None
            if needs_approval and approval is None:
                outcome.skipped = f"approval budget used up after {index} call(s)"
                return
            params = render(call.request.json_body, from_mapping(variables))
            tags = list(call.tags)
            try:
                if call.request.auth == "default":
                    rpc = await client.call(method, _params_for(call.endpoint, params))
                else:
                    rpc = await self._auth_probe(client, call, method, params)
                    tags.append(f"auth:{call.request.auth}")
            except NOT_SENT:
                self._refund(approval)
                raise
            outcome.exchanges.append(
                self.save(rpc, call.endpoint, call.purpose, call.id, tags=tags)
            )
            outcome.statuses.append(rpc.status)
            outcome.answers.append(answer_of(rpc.response))
            for name, path in call.capture.items():
                captured = jsonpath.get_one(rpc.response, path)
                if captured is not None:
                    variables[name] = captured


def answer_of(reply: JsonValue) -> str:
    """How a JSON-RPC reply answered: ``ok``, ``isError`` (a tool error) or ``error <code>``."""
    if not isinstance(reply, dict):
        return "no reply"
    error = reply.get("error")
    if isinstance(error, dict):
        return f"error {error.get('code')}"
    result = reply.get("result")
    return "isError" if isinstance(result, dict) and result.get("isError") is True else "ok"


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
