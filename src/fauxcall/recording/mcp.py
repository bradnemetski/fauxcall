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
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx2

from fauxcall import jsonfmt, jsonpath
from fauxcall.errors import CredentialsRefusedError, HttpStatusError, RecordingError
from fauxcall.logs import get_logger
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient, RpcCall, Transport, replier
from fauxcall.model.api import RoutingModel
from fauxcall.model.auth import AuthModel, AuthScheme, OAuthModel
from fauxcall.model.errors import ErrorTemplate
from fauxcall.model.mcp import (
    PROBE_PROMPT,
    PROBE_RESOURCE,
    PROBE_TOOL,
    McpModel,
    McpToolModel,
    McpTransportModel,
    id_format,
)
from fauxcall.model.plan import Approval, Approvals
from fauxcall.model.recording import (
    CANCELLED,
    Body,
    CapturedRequest,
    CapturedResponse,
    Exchange,
    StreamEvent,
)
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
REFUSAL_PROBE_ID: Final = 9_000_003
UNKNOWN_SESSION: Final = "fauxcall-unknown-session"
UNKNOWN_ROUTE: Final = "/fauxcall-probe-unknown-route"
"""A path no server serves, to record its 404 page."""
PROBE_ORIGIN: Final = "https://fauxcall.invalid"
PROBE_VERSION: Final = "1999-01-01"
"""A protocol version no server speaks, to record how it refuses one."""
LIMIT_PROBE_ID: Final = 9_100_000
SDK_MAX_BATCH: Final = 100
SDK_MAX_BODY_BYTES: Final = 4 * 1024 * 1024
"""The MCP TypeScript SDK's limits (Streamable HTTP), probed during introspection."""
PROBE_REQUEST_HEADERS: Final = "content-type,mcp-session-id"
"""A session id no server issued, sent to learn how the server refuses it."""
KEEP_ALIVE_PROBE_SECONDS: Final = 20.0
"""How long introspection waits for an idle stream's keep-alive (the SDKs send one every 15 s)."""
AUTH_PROBE_ID: Final = 9_000_002
_HTTP_OK: Final = 200
_HTTP_NOT_ALLOWED: Final = 405
_HTTP_ERROR: Final = 400
_HTTP_REDIRECT: Final = 300
_TARGET_PARAM: Final = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
    "resources/subscribe": "uri",
    "resources/unsubscribe": "uri",
}
"""Methods whose endpoint id names their target (``MCP tools/call:<name>``): the param it fills."""


def _learn_from_stream(transport: McpTransportModel, watched: RpcCall) -> None:
    """The keep-alive of an idle GET stream, and the member order of what the server pushed."""
    if watched.body.startswith(":"):
        transport.keep_alive = watched.body
        transport.keep_alive_seconds = max(round(watched.elapsed_ms / 1000, 1), 0.1)
    pushed = next((m for m in watched.messages if isinstance(m, dict) and "method" in m), None)
    if pushed is not None:  # what the server sends on its own, in its member order
        transport.message_member_order = list(pushed)


def _rendered(refusal: JsonValue) -> str:
    """A recorded refusal's body as the probe got it (its version back in place)."""
    return str(_member(refusal, "body") or "").replace("{{value}}", PROBE_VERSION)


def _member(value: JsonValue, key: str) -> JsonValue:
    return value.get(key) if isinstance(value, dict) else None


def _route_template(name: str, call: RpcCall, method: str, path: str) -> ErrorTemplate:
    """The server's answer to a route it doesn't serve, with the path and method as placeholders."""
    text = call.body.replace(f"{method} {path}", "{{method}} {{path}}").replace(path, "{{path}}")
    body_json: JsonValue = None
    with contextlib.suppress(ValueError):
        body_json = jsonfmt.loads(text) if text.lstrip().startswith(("{", "[")) else None
    return ErrorTemplate(
        name=name,
        status=call.status,
        content_type=call.content_type,
        body_json=body_json,
        body_text=None if body_json is not None else text,
        examples=[],
    )


def _cors_headers(call: RpcCall) -> list[list[str]]:
    """The CORS headers of an answer, with the probe's origin and requested headers generalized."""
    found: list[list[str]] = []
    for name, value in call.response_headers:
        lowered = name.lower()
        if not lowered.startswith("access-control-") and lowered != "vary":
            continue
        general = value.replace(PROBE_ORIGIN, "{{origin}}")
        found.append([lowered, general.replace(PROBE_REQUEST_HEADERS, "{{request_headers}}")])
    return found


def _sse_format(body: str) -> tuple[list[str], str | None]:
    """The field order and event name of the first SSE event carrying data."""
    for block in body.replace("\r\n", "\n").split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        fields = [line.split(":", 1)[0] for line in lines]
        data = [line.split(":", 1)[1].strip() for line in lines if line.startswith("data:")]
        if not data or not any(data):
            continue
        order = list(dict.fromkeys(f for f in fields if f in {"id", "event", "data"}))
        name = next(
            (line.split(":", 1)[1].strip() for line in lines if line.startswith("event:")), None
        )
        return order, name
    return [], None


def _priming_since(asked: Mapping[str, bool]) -> str | None:
    """The lowest version whose handshake primed, for servers that compare versions.

    ``None`` unless every version from it up primed and none below it did.
    """
    primed = [version for version, did in asked.items() if did]
    if not primed:
        return None
    lowest = min(primed)
    return lowest if all(did == (version >= lowest) for version, did in asked.items()) else None


def _refusal(call: RpcCall) -> JsonValue:
    """A recorded HTTP refusal, as the fake repeats it.

    The probe's protocol version becomes ``{{value}}``: the fake names the version the client
    sent, as the real server does.
    """
    body = call.body.replace(PROBE_VERSION, "{{value}}")
    return {"status": call.status, "content_type": call.content_type, "body": body}


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


def _member_order(call: RpcCall) -> list[str]:
    """The order the server writes a response's members in (``result``, ``jsonrpc``, ``id``)."""
    return list(call.response) if isinstance(call.response, dict) else []


def _primes(call: RpcCall) -> bool:
    """Whether an SSE answer opened with an empty priming event (an id, no data)."""
    first = call.sse_events[0] if call.sse_events else None
    return first is not None and not first[2].strip() and first[3] is not None


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
    routing: RoutingModel | None = None
    """How its HTTP layer matches paths and answers unknown ones (with ``route_errors``)."""
    route_errors: list[ErrorTemplate] = field(default_factory=list)


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
        self.keep_alive_within = KEEP_ALIVE_PROBE_SECONDS
        self._routing: tuple[RoutingModel, list[ErrorTemplate]] | None = None

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
        path = call.path or parts.path or "/"
        url, safe_path, _, query, headers, sent, notes = self.privacy.request(
            url=urlunsplit(parts._replace(path=path)),
            path=path,
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
                method=HttpMethod(call.http_method),
                url=url,
                path=safe_path,
                query=query,
                headers=headers,
                body=self._sent_body(call, sent, redacted, pseudonymized),
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

    def _sent_body(
        self, call: RpcCall, sent: JsonValue, redacted: list[str], pseudonymized: list[str]
    ) -> Body:
        """The request body as recorded: the JSON-RPC sent, or a probe's raw bytes."""
        if call.sent is not None:
            return Body(kind="json", content_type="application/json", json_value=sent)
        if not call.raw:
            return Body(kind="empty")
        content_type = next(
            (v for k, v in call.request_headers if k.lower() == "content-type"), None
        )
        text = call.raw.decode("utf-8", errors="replace")
        return Body(
            kind="text",
            content_type=content_type,
            text=private_text(self.privacy, text, "request", redacted, pseudonymized),
        )

    # -- introspection -----------------------------------------------------------------------------

    async def _connect(
        self, capabilities: JsonObject | None = None
    ) -> tuple[McpWireClient, RpcCall]:
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
                    return client, await client.initialize(capabilities=capabilities)
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
                with contextlib.suppress(_BudgetExhaustedError):
                    model.transport.refusals = await self._probe_refusals(client, exchanges)
                    await self._probe_streams(client, model.transport, exchanges)
                with contextlib.suppress(_BudgetExhaustedError, httpx2.HTTPError):
                    self._routing = await self._probe_http_layer(client, model, exchanges)
                await self._capability_tools(model, exchanges)
        finally:
            status = await self._close(client)
        if status is not None:
            model.transport.delete_status = status
        if probe_versions and model.transport.kind == "streamable_http":
            primed = model.transport.priming_versions
            asked: dict[str, bool] = {}
            model.transport.protocol_versions = await self._probe_versions(exchanges, primed, asked)
            model.transport.priming_versions = sorted(set(primed))
            model.transport.priming_since = _priming_since(asked)
        counts = (len(model.tools), len(model.resources), len(model.prompts))
        notes = ["{} tools, {} resources, {} prompts".format(*counts)]
        found = McpIntrospection(model=model, exchanges=exchanges, notes=notes)
        if self._routing is not None:
            found.routing, found.route_errors = self._routing
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
        early: list[JsonValue] | None = None
        for label, declared in probes:
            listed, before = await self._tools_for(label, declared, exchanges)
            early = before if label == _ALL else early
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
        if unlocked and early is not None:  # listed before notifications/initialized?
            names = {d.get("name") for d in early if isinstance(d, dict)}
            model.capability_tools_after_initialized = not names & set(unlocked)

    def _spent(self) -> bool:
        return self.calls_made >= self.max_calls

    async def _tools_for(
        self, label: str, declared: JsonObject, exchanges: list[str]
    ) -> tuple[list[JsonValue] | None, list[JsonValue] | None]:
        """The tools listed to a client declaring ``declared`` (``None``: the probe failed).

        The session declaring all capabilities also lists them before it sends
        ``notifications/initialized`` (second value): some servers add their capability tools
        only then.
        """
        client = self._client()
        early: list[JsonValue] | None = None
        calls: list[RpcCall] = []
        try:
            await self._pace()
            await client.initialize(capabilities=declared, announce=label != _ALL)
            if label == _ALL:
                early, before = await client.list_all("tools/list", "tools", before_page=self._pace)
                exchanges += [
                    self.save(c, method_endpoint("tools/list", f"with-{label}-before-initialized"))
                    for c in before
                ]
                await client.notify("notifications/initialized")
            tools, calls = await client.list_all("tools/list", "tools", before_page=self._pace)
        except _BudgetExhaustedError:
            return None, early  # the recording budget is spent: fewer probes, not a failed init
        except (RecordingError, httpx2.HTTPError, TimeoutError) as error:
            log.info("mcp_capability_probe_failed", capability=label, error=str(error))
            return None, early
        finally:
            await self._close(client)
        exchanges += [self.save(c, method_endpoint("tools/list", f"with-{label}")) for c in calls]
        return tools, early

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
            member_order=_member_order(init),
            session_id_format=id_format(client.session_id),
            event_id_format=id_format(next((i for *_, i in init.sse_events if i), None)),
        )
        if _primes(init) and client.negotiated_version:
            transport.priming_versions = [client.negotiated_version]
        if response_mode == "sse" and not legacy:
            fields, name = _sse_format(init.body)
            if fields:
                transport.sse_field_order, transport.sse_event_name = fields, name
            transport.sse_streamed = init.header("content-length") is None
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

    async def _probe_refusals(
        self, client: McpWireClient, exchanges: list[str]
    ) -> dict[str, JsonValue]:
        """How the server refuses requests at the HTTP level, which the fake repeats.

        A client that won't take SSE answers, a body that isn't JSON, an unknown protocol
        version header, and a session id the server never issued.
        """
        probe = protocol.request(REFUSAL_PROBE_ID, "tools/list")
        situations: dict[str, tuple[dict[str, str], bool]] = {
            "accept": ({"accept": "application/json"}, True),
            "content_type": ({"content-type": "text/plain"}, True),
            "protocol_header": ({protocol.PROTOCOL_HEADER: PROBE_VERSION}, True),
            "unknown_session": ({protocol.SESSION_HEADER: UNKNOWN_SESSION}, False),
        }
        refusals: dict[str, JsonValue] = {}
        for situation, (headers, with_session) in situations.items():
            if situation == "unknown_session" and client.session_id is None:
                continue
            await self._pace()
            call = await client.post(probe, include_session=with_session, extra_headers=headers)
            # "discovery": a replay can't send the odd headers that drew these refusals
            exchanges.append(self.save(call, method_endpoint("tools/list", situation)))
            if call.status >= _HTTP_ERROR:
                refusals[situation] = _refusal(call)
        return refusals

    async def _probe_streams(
        self, client: McpWireClient, transport: McpTransportModel, exchanges: list[str]
    ) -> None:
        """How the server refuses GETs and DELETEs it won't serve, and keeps streams alive.

        A GET and a DELETE for a session it never issued, a second GET stream for the same
        session, and the comment it writes to an idle stream (waited for, at most
        ``keep_alive_within`` seconds).
        """
        await self._probe_session_refusals(client, transport, exchanges)
        if transport.get_stream != "sse":
            return
        await self._pace()
        await self._pace()  # the stream, and a second one meanwhile
        comment, second = await client.watch_stream(self.keep_alive_within)
        if second is not None:
            exchanges.append(self.save(second, method_endpoint("GET", "second_stream")))
            if second.status >= _HTTP_ERROR:
                transport.refusals["get_second_stream"] = _refusal(second)
        if comment is not None:
            exchanges.append(self.save(comment, method_endpoint("GET", "keep_alive")))
            _learn_from_stream(transport, comment)

    async def _probe_session_refusals(
        self, client: McpWireClient, transport: McpTransportModel, exchanges: list[str]
    ) -> None:
        """A GET and a DELETE for a session the server never issued, and GETs it refuses."""
        if client.session_id is None:
            return
        for http_method, situation in (
            ("GET", "get_unknown_session"),
            ("DELETE", "delete_unknown_session"),
        ):
            if http_method == "GET" and transport.get_stream != "sse":
                continue
            await self._pace()
            call = await client.probe_http(http_method, UNKNOWN_SESSION)
            exchanges.append(self.save(call, method_endpoint(http_method, situation)))
            if call.status >= _HTTP_ERROR:
                transport.refusals[situation] = _refusal(call)
        if transport.get_stream == "sse":
            for situation, headers in (
                ("get_accept", {"accept": "application/json"}),
                ("get_protocol_header", {protocol.PROTOCOL_HEADER: PROBE_VERSION}),
            ):
                await self._pace()
                call = await client.probe_http("GET", client.session_id, headers=headers)
                exchanges.append(self.save(call, method_endpoint("GET", situation)))
                if call.status >= _HTTP_ERROR:
                    transport.refusals[situation] = _refusal(call)

    async def _probe_http_layer(
        self, client: McpWireClient, model: McpModel, exchanges: list[str]
    ) -> tuple[RoutingModel, list[ErrorTemplate]] | None:
        """How the server's HTTP layer answers outside MCP itself.

        A path it doesn't serve (its 404 page, with the path and method as placeholders), the MCP
        path in upper case and with a trailing slash, another method on it, a CORS preflight and
        the CORS headers of ordinary answers, bodies that aren't JSON or JSON-RPC, and the order
        in which it checks a POST. Returns the routing and its error templates.
        """
        transport = model.transport
        path = transport.path

        async def probe(
            method: str,
            at: str | None,
            situation: str,
            headers: Mapping[str, str] | None = None,
        ) -> RpcCall:
            await self._pace()
            call = await client.probe_http(method, None, path=at, headers=headers)
            exchanges.append(self.save(call, method_endpoint(f"{method} {at or path}", situation)))
            return call

        plain = await probe("GET", None, "no_session")
        unknown = await probe(
            "GET", UNKNOWN_ROUTE, "unknown_route", headers={"origin": PROBE_ORIGIN}
        )
        routing = RoutingModel()
        errors: list[ErrorTemplate] = []
        if unknown.status >= _HTTP_ERROR:
            errors.append(_route_template("unknown_route", unknown, "GET", UNKNOWN_ROUTE))
            routing.unknown_route_template = "unknown_route"

        def like_mcp(call: RpcCall) -> bool:
            return (call.status, call.body) == (plain.status, plain.body)

        if path.upper() != path:
            routing.case_insensitive = like_mcp(await probe("GET", path.upper(), "upper_case"))
        slashed = await probe("GET", path.rstrip("/") + "/", "trailing_slash")
        routing.trailing_slash = (
            "ignore"
            if like_mcp(slashed)
            else "redirect"
            if _HTTP_REDIRECT <= slashed.status < _HTTP_ERROR
            else "strict"
        )
        other = await probe("PUT", None, "method_not_allowed")
        if other.status >= _HTTP_ERROR:
            errors.append(_route_template("method_not_allowed", other, "PUT", path))
            routing.method_not_allowed_template = "method_not_allowed"
        transport.cors = await self._probe_cors(probe, plain, unknown)
        await self._probe_bodies(client, transport, exchanges)
        return routing, errors

    async def _probe_cors(
        self,
        probe: Callable[..., Awaitable[RpcCall]],
        plain: RpcCall,
        unknown: RpcCall,
    ) -> JsonValue:
        """CORS as the server answers it, or ``None`` when it sends no CORS headers."""
        preflight = await probe(
            "OPTIONS",
            None,
            "cors_preflight",
            headers={
                "origin": PROBE_ORIGIN,
                "access-control-request-method": "POST",
                "access-control-request-headers": PROBE_REQUEST_HEADERS,
            },
        )
        found = {
            "preflight": {"status": preflight.status, "headers": _cors_headers(preflight)},
            "headers": _cors_headers(unknown),
            "headers_without_origin": _cors_headers(plain),
        }
        if not any(found["preflight"]["headers"]) and not found["headers"]:  # type: ignore[index]
            return None
        return found  # type: ignore[return-value]

    async def _probe_bodies(
        self, client: McpWireClient, transport: McpTransportModel, exchanges: list[str]
    ) -> None:
        """Bodies the server refuses before MCP (not JSON, not JSON-RPC), and its check order."""
        good = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        for situation, body in (("invalid_json", b"{"), ("not_jsonrpc", b'{"fauxcall": "probe"}')):
            await self._pace()
            call = await client.probe_http("POST", client.session_id, headers=good, content=body)
            exchanges.append(self.save(call, method_endpoint("tools/list", situation)))
            if call.status >= _HTTP_ERROR:
                transport.refusals[situation] = _refusal(call)
        transport.refusal_order = await self._probe_refusal_order(client, transport, exchanges)
        await self._probe_limits(client, transport, exchanges)

    async def _probe_limits(
        self, client: McpWireClient, transport: McpTransportModel, exchanges: list[str]
    ) -> None:
        """The SDKs' default limits, where the server keeps them.

        A batch of 100 messages (101 refused) and a 4 MiB body; other limits come from the
        server's docs.
        """
        good = {"content-type": "application/json", "accept": "application/json, text/event-stream"}

        async def send(content: bytes, situation: str) -> RpcCall:
            await self._pace()
            call = await client.probe_http("POST", client.session_id, headers=good, content=content)
            exchanges.append(self.save(call, method_endpoint("ping", situation)))
            return call

        def batch(size: int) -> bytes:
            pings: list[JsonValue] = [
                protocol.request(LIMIT_PROBE_ID + i, "ping") for i in range(size)
            ]
            return jsonfmt.dumps(pings).encode()

        over = await send(batch(SDK_MAX_BATCH + 1), "batch-over")
        if (
            over.status >= _HTTP_ERROR
            and (await send(batch(SDK_MAX_BATCH), "batch")).status < _HTTP_ERROR
        ):
            transport.max_batch = SDK_MAX_BATCH
            transport.refusals["batch_too_large"] = _refusal(over)
        padding = "x" * SDK_MAX_BODY_BYTES
        big = jsonfmt.dumps(protocol.request(LIMIT_PROBE_ID, "ping", {"_meta": {"pad": padding}}))
        oversized = await send(big.encode(), "body-over")
        if oversized.status >= _HTTP_ERROR:
            transport.max_body_bytes = SDK_MAX_BODY_BYTES
            transport.refusals["body_too_large"] = _refusal(oversized)

    async def _probe_refusal_order(
        self, client: McpWireClient, transport: McpTransportModel, exchanges: list[str]
    ) -> list[str]:
        """Which refusal wins when a POST breaks several rules.

        Break them all, see which answers, stop breaking that one, and repeat.
        """
        breaks = {
            "unknown_session": ({protocol.SESSION_HEADER: UNKNOWN_SESSION}, None),
            "accept": ({"accept": "application/json"}, None),
            "content_type": ({"content-type": "text/plain"}, None),
            "protocol_header": ({protocol.PROTOCOL_HEADER: PROBE_VERSION}, None),
            "invalid_json": ({}, b"{"),
        }
        known = {k: v for k, v in transport.refusals.items() if k in breaks and isinstance(v, dict)}
        remaining = list(known)
        order: list[str] = []
        while len(remaining) > 1:
            headers = {
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            }
            body = jsonfmt.dumps(protocol.request(REFUSAL_PROBE_ID, "tools/list")).encode()
            for situation in remaining:
                extra, raw = breaks[situation]
                headers |= extra
                body = raw if raw is not None else body
            await self._pace()
            call = await client.probe_http("POST", client.session_id, headers=headers, content=body)
            exchanges.append(self.save(call, method_endpoint("tools/list", "refusal-order")))
            winner = next(
                (
                    situation
                    for situation in remaining
                    if (call.status, call.body)
                    == (_member(known[situation], "status"), _rendered(known[situation]))
                ),
                None,
            )
            if winner is None:
                break  # an answer none of them gives alone: order unknown from here
            order.append(winner)
            remaining.remove(winner)
        return [*order, *remaining] if order else []

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

    async def _probe_versions(
        self,
        exchanges: list[str],
        primed: list[str] | None = None,
        asked: dict[str, bool] | None = None,
    ) -> dict[str, str]:
        """Which protocol version the server answers to each known version it is offered.

        Versions whose handshake opened with a priming event are added to ``primed``; ``asked``
        maps each version offered to whether its handshake primed.
        """
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
                    if _primes(call) and primed is not None:
                        primed.append(client.negotiated_version)
                    if asked is not None:
                        asked[version] = _primes(call)
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
        The session declares the plan's ``client_capabilities``; requests the server sends during
        a call (sampling …) are answered with the call's ``client_replies``.
        """
        report = RunReport(plan=plan.name)
        definitions = tools or {}
        variables: dict[str, JsonValue] = {}
        declared = plan.client_capabilities if isinstance(plan.client_capabilities, dict) else None
        client, init = await self._connect(declared)
        try:
            self.save(init, method_endpoint("initialize"), purpose="sample")
            if plan.listen:
                await self._pace()
                client.on_request = replier({})  # until a call says how to answer
                await client.listen()
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
            if plan.listen:
                await self._heard(client, plan.listen_after_seconds)
            await self._close(client)
            report.calls_made = self.calls_made
        return report

    async def _heard(self, client: McpWireClient, linger: float) -> None:
        """Stop listening on the client's GET stream (after ``linger`` seconds) and save it."""
        if linger:
            await self._sleep(linger)
        heard = await client.stop_listening()
        if heard is not None:
            self.save(heard, method_endpoint("GET", "stream"), purpose="sample")

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
            client.on_request = replier(call.client_replies)
            try:
                if call.request.auth == "default":
                    rpc = await client.call(
                        method,
                        _params_for(call.endpoint, params),
                        cancel_after=None
                        if call.cancel_after_ms is None
                        else call.cancel_after_ms / 1000,
                    )
                    if rpc.cancelled_after_ms is not None:
                        tags.append(CANCELLED)
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
