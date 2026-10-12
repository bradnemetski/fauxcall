"""The fake's runtime: one :class:`FakeRuntime` per app, and :func:`create_app` to build the server.

Every request goes through the same pipeline::

    faults -> auth -> rate limits -> parameters -> role handler (or custom handler)
           -> contract/rate-limit headers -> latency -> journal

Generated fakes declare one explicit route per endpoint that calls :func:`handle`.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, Response, StreamingResponse

from fauxcall import __version__
from fauxcall.calendar import TradingCalendar
from fauxcall.clock import ClockRegistry, ControlledClock, ns_to_datetime
from fauxcall.config import FakeSettings
from fauxcall.errors import FauxcallError
from fauxcall.logs import (
    REDACTED,
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
    is_secret_key,
)
from fauxcall.model.api import ApiModel
from fauxcall.runtime.admin import admin_router
from fauxcall.runtime.auth import Authenticator
from fauxcall.runtime.context import EarlyResponse, RequestContext, request_rng
from fauxcall.runtime.faults import FaultInjector
from fauxcall.runtime.handlers import ROLE_HANDLERS
from fauxcall.runtime.jev import JevGateway
from fauxcall.runtime.jobs import JobEngine
from fauxcall.runtime.journal import Journal, JournalEntry, body_for_journal
from fauxcall.runtime.mcp_server import McpFake
from fauxcall.runtime.oauth import oauth_router, www_authenticate
from fauxcall.runtime.params import ParamProblem, parse_params
from fauxcall.runtime.ratelimit import RateLimiter
from fauxcall.runtime.recordings import RecordingIndex
from fauxcall.runtime.routing import RoutingQuirks
from fauxcall.runtime.series import SeriesStore
from fauxcall.runtime.streaming import (
    chunked_response,
    pick_stream_exchange,
    replay_websocket,
    sse_response,
)
from fauxcall.runtime.templating import render_text
from fauxcall.storage import read_model
from fauxcall.world import World

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence

    from fastapi import APIRouter
    from starlette.requests import HTTPConnection
    from starlette.websockets import WebSocket

    from fauxcall.config import FaultRule
    from fauxcall.model.endpoints import EndpointModel
    from fauxcall.model.errors import ErrorCondition
    from fauxcall.model.jobs import JobModel
    from fauxcall.model.recording import Exchange
    from fauxcall.runtime.auth import AuthFailure, Identity
    from fauxcall.runtime.mcp_server import ToolHandler
    from fauxcall.types import JsonValue

    Handler = Callable[[RequestContext], Awaitable[Response]]
    WebSocketHandler = Callable[[WebSocket, RequestContext], Awaitable[None]]

log = get_logger(__name__)

_AUTH_CONDITIONS: Final[dict[str, ErrorCondition]] = {
    "missing": "unauthorized",
    "invalid": "unauthorized",
    "forbidden": "forbidden",
    "expired": "expired",
    "insufficient_scope": "insufficient_scope",
}


@dataclass(frozen=True, slots=True)
class Blob:
    """Bytes uploaded to the fake (kept per session, served by downloads)."""

    content: bytes
    content_type: str | None
    filename: str | None


class FakeRuntime:
    """Everything a running fake needs."""

    def __init__(
        self,
        settings: FakeSettings,
        model: ApiModel,
        *,
        world: World | None = None,
        clock: ControlledClock | None = None,
        jev: JevGateway | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.settings = settings
        self.model = model
        data = settings.data_dir
        self.world = world if world is not None else World(model, data)
        self._state_file = settings.resolved_state_dir / "world.json"
        if settings.persist_state and self._state_file.exists():
            self.world.load_state(self._state_file)
        self.clocks = ClockRegistry(clock or ControlledClock.from_settings(settings.clock))
        self._calendars = {c.name: TradingCalendar(c) for c in model.calendars}
        self.journal = Journal(settings.journal_limit)
        self.faults = FaultInjector(settings.faults, settings.seed)
        self.rate_limiter = RateLimiter(monotonic_ns)
        self.auth = Authenticator(model.auth, settings.auth)
        self.jobs = JobEngine(settings.seed)
        self.jev = jev if jev is not None else JevGateway(settings, data / "jev-cache.jsonl")
        self.recordings = RecordingIndex(data / "recordings")
        self.series = SeriesStore(data / "series", origin_ns=self.clocks.default.now_ns())
        self._sleep = sleep
        self._counters: dict[tuple[str, str], int] = {}
        self._session_seq: dict[str, int] = {}
        self._job_outcomes: dict[tuple[str, str, str], str] = {}
        self._blobs: dict[tuple[str, str, str], Blob] = {}
        self._pending: dict[str, Blob] = {}
        self._handler_state: dict[str, dict[str, Any]] = {}
        self.mcp: McpFake | None = None
        self._secret_names = frozenset(
            scheme.name.lower()
            for scheme in (model.auth.schemes if model.auth else [])
            if scheme.name
        )

    @classmethod
    def from_settings(cls, settings: FakeSettings) -> FakeRuntime:
        """Load ``data/model.json`` and build the runtime."""
        model = read_model(settings.data_dir / "model.json", ApiModel)
        model.check()
        return cls(settings, model)

    # -- small services used by contexts and handlers ---------------------------------------------

    def redact(self, pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        """Header or query pairs for the journal, with every credential blanked out.

        Besides names that look secret, this covers the names the model's auth schemes use (an
        ``appid`` query parameter, say).
        """
        return [
            (name, REDACTED if is_secret_key(name) or name.lower() in self._secret_names else value)
            for name, value in pairs
        ]

    async def pause(self, seconds: float) -> None:
        """Sleep through the runtime's (injectable) sleep: emulated latency and fault delays."""
        await self._sleep(seconds)

    def calendar(self, name: str) -> TradingCalendar:
        """A calendar by name."""
        if name not in self._calendars:
            self._calendars[name] = TradingCalendar(self.model.calendar(name))
        return self._calendars[name]

    def next_counter(self, session: str, key: str, start: int, step: int) -> int:
        """Next value of a per-session counter."""
        current = self._counters.get((session, key))
        value = start if current is None else current + step
        self._counters[session, key] = value
        return value

    def job_outcome(self, session: str, job: str, key: str) -> str | None:
        """An outcome decided at submission (e.g. by Jev), if any."""
        return self._job_outcomes.get((session, job, key))

    def set_job_outcome(self, session: str, job: str, key: str, outcome: str) -> None:
        """Remember a decided outcome."""
        self._job_outcomes[session, job, key] = outcome

    async def decide_outcome(self, ctx: RequestContext, job: JobModel) -> str | None:
        """Ask Jev for the outcome when the job is configured that way (``None`` = seeded)."""
        spec = ctx.endpoint.semantic.outcome if ctx.endpoint and ctx.endpoint.semantic else None
        if job.outcome_decider != "jev" or spec is None:
            return None
        state: JsonValue = {name: ctx.request_value(name) for name in spec.inputs}
        answer = await self.jev.choose(
            instructions=spec.instructions, options=spec.outcomes, state=state
        )
        decided = (
            answer.value if answer is not None and isinstance(answer.value, str) else spec.fallback
        )
        terminals = {s.name for s in job.states if s.terminal}
        return decided if decided in terminals else None

    def pending_blob(
        self,
        ctx: RequestContext,
        field: str,
        content: bytes,
        content_type: str | None,
        filename: str | None,
    ) -> None:
        """Hold uploaded bytes until the record they belong to gets its key."""
        del field
        self._pending[ctx.request_id] = Blob(content, content_type, filename)

    def commit_blob(self, ctx: RequestContext, collection: str, key: JsonValue) -> None:
        """Attach pending uploaded bytes to a record."""
        blob = self._pending.pop(ctx.request_id, None)
        if blob is not None:
            self._blobs[ctx.session, collection, str(key)] = blob

    def blob(self, ctx: RequestContext, collection: str, key: JsonValue) -> Blob | None:
        """Uploaded bytes of a record in this session."""
        return self._blobs.get((ctx.session, collection, str(key)))

    def drop_blob(self, ctx: RequestContext, collection: str, key: JsonValue) -> None:
        """Forget uploaded bytes (record deleted)."""
        self._blobs.pop((ctx.session, collection, str(key)), None)

    def handler_state(self, session: str) -> dict[str, Any]:
        """Free-form state custom handlers keep for one session (``ctx.state``), until reset."""
        return self._handler_state.setdefault(session, {})

    def reset(self, session: str | None = None, *, journal: bool = False) -> None:
        """Back to the recorded seed (one session or all), including clocks, counters, limits."""
        self.world.reset(session)
        self.clocks.reset(session)
        self.faults.reset_counts(session)
        self.rate_limiter.reset()
        self._counters = {
            k: v for k, v in self._counters.items() if session is not None and k[0] != session
        }
        self._job_outcomes = {
            k: v for k, v in self._job_outcomes.items() if session is not None and k[0] != session
        }
        self._blobs = {
            k: v for k, v in self._blobs.items() if session is not None and k[0] != session
        }
        if session is None:
            self._session_seq.clear()
            self._handler_state.clear()
        else:
            self._session_seq.pop(session, None)
            self._handler_state.pop(session, None)
        if journal:
            self.journal.clear(session)
        self.persist()

    def persist(self) -> None:
        """Save world state when persistence is enabled."""
        if self.settings.persist_state:
            self.world.save_state(self._state_file)

    # -- the pipeline ---------------------------------------------------------------------------

    def session_of(self, connection: HTTPConnection) -> str:
        """The evaluation session a request belongs to."""
        return connection.headers.get(self.settings.session_header) or "default"

    def new_context(
        self, connection: HTTPConnection, endpoint: EndpointModel | None
    ) -> RequestContext:
        """A context for one request; the clock is read once here."""
        session = self.session_of(connection)
        seq = self._session_seq.get(session, 0) + 1
        self._session_seq[session] = seq
        request_id = (
            connection.headers.get("x-request-id")
            or hashlib.sha256(f"{session}:{seq}:{time.time_ns()}".encode()).hexdigest()[:16]
        )
        return RequestContext(
            runtime=self,
            request=connection,
            endpoint=endpoint,
            session=session,
            request_id=request_id,
            now_ns=self.clocks.for_session(session).now_ns(),
            rng=request_rng(self.settings.seed, session, seq),
            seq=self.journal.next_seq(),
        )

    async def handle(
        self, request: Request, endpoint_id: str, impl: Handler | None = None
    ) -> Response:
        """Answer one HTTP request for ``endpoint_id`` (``impl`` replaces the role handler)."""
        started = time.perf_counter()
        endpoint = self.model.endpoint(endpoint_id)
        ctx = self.new_context(request, endpoint)
        bind_context(request_id=ctx.request_id, session=ctx.session, endpoint=endpoint_id)
        try:
            await ctx.load_body()
            response = await self._run(ctx, impl)
        except EarlyResponse as early:
            response = early.response
        except FauxcallError as error:  # the model or world can't answer this: a fake bug
            log.exception("fake_error", endpoint=endpoint_id)
            body: JsonValue = {"error": "fauxcall_error", "message": str(error)}
            response = ctx.respond(body, status=500, content_type="application/json")
        await self._apply_latency(ctx, endpoint, started, error=response.status_code >= 400)  # noqa: PLR2004
        self._record(ctx, response, started)
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            self.persist()
        clear_context()
        return response

    async def _run(self, ctx: RequestContext, impl: Handler | None) -> Response:
        endpoint = ctx.endpoint
        assert endpoint is not None  # noqa: S101
        fault = self.faults.pick(endpoint.id, ctx.session)
        if fault is not None:
            ctx.fault = fault.kind
            if fault.kind == "delay":
                await self._sleep(fault.delay_ms / 1000)
            else:
                return await self._fault_response(ctx, fault)
        request = ctx.request
        outcome = self.auth.authenticate(
            headers=dict(request.headers),
            query=dict(request.query_params),
            cookies=dict(request.cookies),
            now_ns=ctx.now_ns,
            required=self.auth.requires_auth(endpoint.id, endpoint.auth_required),
        )
        ctx.identity = outcome.identity
        if outcome.failure is not None:
            return self._auth_error(ctx, outcome.failure)
        limited = self._rate_limit(ctx)
        if limited is not None:
            return limited
        if self._lacks_scopes(ctx):
            return self._auth_error(ctx, "insufficient_scope")
        try:
            ctx.params = parse_params(endpoint, request)  # type: ignore[arg-type]
        except ParamProblem as problem:
            return ctx.error(problem.condition, param=problem.param, value=problem.value)
        if impl is not None:
            return await impl(ctx)
        if endpoint.role == "sse":
            return self._stream(ctx)
        return await ROLE_HANDLERS[endpoint.role](ctx)

    def _stream(self, ctx: RequestContext) -> Response:
        endpoint = ctx.endpoint
        assert endpoint is not None  # noqa: S101
        assert endpoint.stream is not None  # noqa: S101
        exchanges = self._stream_exchanges(endpoint)
        chosen = pick_stream_exchange(exchanges, dict(ctx.request.query_params))
        if chosen is None:
            return ctx.error("not_found")
        if endpoint.stream.kind == "chunked":
            return chunked_response(ctx, chosen, speed=endpoint.stream.speed, sleep=self._sleep)
        return sse_response(ctx, chosen, speed=endpoint.stream.speed, sleep=self._sleep)

    def _stream_exchanges(self, endpoint: EndpointModel) -> list[Exchange]:
        assert endpoint.stream is not None  # noqa: S101
        recorded = self.recordings.for_endpoint(endpoint.id)
        if endpoint.stream.exchanges:
            wanted = set(endpoint.stream.exchanges)
            recorded = [e for e in recorded if e.id in wanted]
        return [e for e in recorded if e.stream]

    async def handle_websocket(
        self, websocket: WebSocket, endpoint_id: str, impl: WebSocketHandler | None = None
    ) -> None:
        """Serve one WebSocket connection for ``endpoint_id``."""
        endpoint = self.model.endpoint(endpoint_id)
        ctx = self.new_context(websocket, endpoint)
        outcome = self.auth.authenticate(
            headers=dict(websocket.headers),
            query=dict(websocket.query_params),
            cookies=dict(websocket.cookies),
            now_ns=ctx.now_ns,
            required=self.auth.requires_auth(endpoint.id, endpoint.auth_required),
        )
        ctx.identity = outcome.identity
        stream = endpoint.stream
        if outcome.failure is not None:
            code = stream.auth_failure_close_code if stream else None
            if code is not None:
                await websocket.accept()
                await websocket.close(code=code)
            else:
                await websocket.close(code=1008)
            return
        if impl is not None:
            await impl(websocket, ctx)
            return
        assert stream is not None  # noqa: S101 - validated by the model for the websocket role
        await replay_websocket(
            websocket,
            ctx,
            self._stream_exchanges(endpoint),
            respond_to=stream.respond_to,
            speed=stream.speed,
            loop=stream.loop,
            sleep=self._sleep,
        )

    async def _fault_response(self, ctx: RequestContext, fault: FaultRule) -> Response:
        match fault.kind:
            case "timeout":
                await self._sleep((fault.delay_ms or 30_000) / 1000)
                return ctx.error("custom", status=504, values={"fault": "timeout"})
            case "disconnect":

                async def broken() -> AsyncIterator[bytes]:
                    yield b""
                    raise ConnectionAbortedError("fauxcall injected disconnect")

                return StreamingResponse(broken(), status_code=200)
            case "rate_limit":
                template = next(
                    (r.error_template for r in self.model.rate_limits if r.error_template), None
                )
                return ctx.error("rate_limited", template=template, headers={"retry-after": "1"})
            case _:
                if fault.error_template:
                    return ctx.error("custom", template=fault.error_template, status=fault.status)
                return ctx.error("custom", status=fault.status, values={"fault": "error"})

    def _lacks_scopes(self, ctx: RequestContext) -> bool:
        """Whether the caller misses a scope the endpoint requires (authorization, after counting).

        Callers without scopes (any well-formed credential, users configured without scopes) are
        unrestricted; only users and tokens that list scopes are held to them.
        """
        auth, identity = self.model.auth, ctx.identity
        if auth is None or identity is None or not identity.scopes or ctx.endpoint is None:
            return False
        return not auth.scopes_for(ctx.endpoint.id) <= set(identity.scopes)

    def _auth_error(self, ctx: RequestContext, failure: AuthFailure) -> Response:
        auth = self.model.auth
        assert auth is not None  # noqa: S101 - failures only happen with an auth model
        templates = {
            "missing": auth.missing_template or auth.invalid_template,
            "invalid": auth.invalid_template or auth.missing_template,
            "forbidden": auth.forbidden_template,
            "expired": auth.expired_template or auth.invalid_template or auth.missing_template,
            "insufficient_scope": auth.insufficient_scope_template or auth.forbidden_template,
        }
        return ctx.error(_AUTH_CONDITIONS[failure], template=templates[failure])

    def _rate_key(self, ctx: RequestContext, kind: str) -> str:
        match kind:
            case "global":
                return "global"
            case "ip":
                return ctx.request.client.host if ctx.request.client else "unknown"
            case "session":
                return ctx.session
            case _:
                if ctx.identity is not None and not ctx.identity.anonymous:
                    return f"user:{ctx.identity.name}"
                raw = ctx.request.headers.get("authorization") or str(
                    sorted(ctx.request.headers.items())
                )
                return "anon:" + hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _rate_limit(self, ctx: RequestContext) -> Response | None:
        if not self.settings.rate_limits_enabled or ctx.endpoint is None:
            return None
        for limit in self.model.rate_limits_for(ctx.endpoint.id):
            status = self.rate_limiter.hit(limit, self._rate_key(ctx, limit.key))
            values: dict[str, JsonValue] = dict(status.header_values(ctx.now_ns))
            resolver = ctx.resolver(**values)
            headers = {name: render_text(t, resolver) for name, t in limit.headers.items()}
            if not status.allowed:
                if limit.retry_after:
                    headers["retry-after"] = str(status.retry_after)
                return ctx.error(
                    "rate_limited", template=limit.error_template, headers=headers, values=values
                )
            if limit.headers_on_success:
                ctx.extra_headers.update(headers)
        return None

    async def _apply_latency(
        self,
        ctx: RequestContext,
        endpoint: EndpointModel | None,
        started: float,
        *,
        error: bool = False,
    ) -> None:
        if not self.settings.latency_enabled or endpoint is None or endpoint.latency is None:
            return
        target_ms = endpoint.latency.pick_ms(ctx.rng, error=error)
        if target_ms is None:
            return
        remaining = target_ms / 1000 - (time.perf_counter() - started)
        if remaining > 0:
            await self._sleep(remaining)

    def _record(self, ctx: RequestContext, response: Response, started: float) -> None:
        if not self.journal.enabled:
            return
        request = ctx.request
        req_body, req_text = body_for_journal(ctx.body_bytes, request.headers.get("content-type"))
        streamed = isinstance(response, StreamingResponse | FileResponse)
        res_body, res_text = (None, None)
        if not streamed:
            res_body, res_text = body_for_journal(
                bytes(response.body), response.headers.get("content-type")
            )
        self.journal.add(
            JournalEntry(
                seq=ctx.seq,
                session=ctx.session,
                request_id=ctx.request_id,
                at=ns_to_datetime(ctx.now_ns),
                method=request.scope.get("method", "WS"),
                path=request.url.path,
                query=self.redact(request.query_params.multi_items()),
                endpoint=ctx.endpoint_id or None,
                request_headers=self.redact(request.headers.items()),
                request_body=req_body,
                request_text=req_text,
                status=response.status_code,
                response_headers=self.redact(response.headers.items()),
                response_body=res_body,
                response_text=res_text,
                streamed=streamed,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                fault=ctx.fault,
                identity=ctx.identity.name if ctx.identity else None,
            )
        )

    # -- unknown routes ----------------------------------------------------------------------------

    async def not_routed(self, request: Request, status: int) -> Response:
        """404 / 405 answered with the real API's templates."""
        started = time.perf_counter()
        ctx = self.new_context(request, None)
        await ctx.load_body()
        routing = self.model.routing
        if status == 405:  # noqa: PLR2004
            response = ctx.error("method_not_allowed", template=routing.method_not_allowed_template)
        else:
            response = ctx.error("unknown_route", template=routing.unknown_route_template)
        self._record(ctx, response, started)
        return response


# -- app factory -----------------------------------------------------------------------------


def runtime_of(connection: HTTPConnection) -> FakeRuntime:
    """The runtime serving this request."""
    runtime: FakeRuntime = connection.app.state.fauxcall
    return runtime


async def handle(request: Request, endpoint_id: str, impl: Handler | None = None) -> Response:
    """Entry point used by generated routes."""
    return await runtime_of(request).handle(request, endpoint_id, impl)


async def handle_websocket(
    websocket: WebSocket, endpoint_id: str, impl: WebSocketHandler | None = None
) -> None:
    """Entry point used by generated WebSocket routes."""
    await runtime_of(websocket).handle_websocket(websocket, endpoint_id, impl)


def create_app(
    settings: FakeSettings | None = None,
    *,
    routers: Sequence[APIRouter] = (),
    runtime: FakeRuntime | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    """Build the FastAPI app for one fake."""
    settings = settings or FakeSettings()
    runtime = runtime or FakeRuntime.from_settings(settings)
    if configure_logs:
        configure_logging(
            settings.log_level, settings.log_format, static={"fake": runtime.model.name}
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info("fake_started", fake=runtime.model.name, endpoints=len(runtime.model.endpoints))
        try:
            yield
        finally:
            runtime.persist()
            await runtime.jev.close()

    app = FastAPI(
        title=f"FauxCall fake: {runtime.model.title}",
        version=__version__,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.fauxcall = runtime
    app.router.redirect_slashes = runtime.model.routing.trailing_slash == "redirect"
    app.include_router(admin_router(), prefix=settings.admin_prefix)
    _add_spec_routes(app, runtime)
    _add_mcp(app, runtime)
    for router in routers:
        app.include_router(router)

    async def http_error(request: Request, exc: Exception) -> Response:
        status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        if status in {404, 405}:
            return await runtime_of(request).not_routed(request, status)
        detail = exc.detail if isinstance(exc, StarletteHTTPException) else "error"
        return Response(content=str(detail), status_code=status)

    async def validation_error(request: Request, exc: Exception) -> Response:
        del exc  # generated routes don't declare validated parameters; keep the real API's 400
        ctx = runtime_of(request).new_context(request, None)
        return ctx.error("invalid_param")

    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_middleware(RoutingQuirks, model=runtime.model, admin_prefix=settings.admin_prefix)
    return app


def register_tool(app: FastAPI, name: str, handler: ToolHandler) -> None:
    """Implement an MCP tool with custom code (``behavior: computed`` in the model)."""
    fake = runtime_of_app(app).mcp
    if fake is None:
        raise ValueError("this fake has no MCP server")
    fake.tool_handlers[name] = handler


def runtime_of_app(app: FastAPI) -> FakeRuntime:
    """The runtime attached to an app."""
    runtime: FakeRuntime = app.state.fauxcall
    return runtime


def _add_mcp(app: FastAPI, runtime: FakeRuntime) -> None:
    model = runtime.model.mcp
    if model is None:
        return
    fake = McpFake(runtime, model)
    runtime.mcp = fake
    transport = model.transport
    oauth = next(
        (
            scheme.oauth
            for scheme in (runtime.model.auth.schemes if runtime.model.auth else [])
            if scheme.oauth
        ),
        None,
    )
    if oauth is not None:
        app.include_router(oauth_router(runtime, oauth, transport.path))

    def guarded(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            identity, failure = _mcp_auth(runtime, request)
            if failure is not None:
                return failure
            request.state.fauxcall_identity = identity  # for tool handlers and the journal
            return await handler(request)

        return endpoint

    if transport.kind == "sse":
        app.add_api_route(
            transport.path, guarded(fake.sse_stream), methods=["GET"], include_in_schema=False
        )
        messages = transport.messages_path or "/messages/"
        app.add_api_route(
            messages, guarded(fake.sse_message), methods=["POST"], include_in_schema=False
        )
    else:
        app.add_api_route(
            transport.path,
            guarded(fake.streamable),
            methods=["GET", "POST", "DELETE"],
            include_in_schema=False,
        )


def _mcp_auth(runtime: FakeRuntime, request: Request) -> tuple[Identity | None, Response | None]:
    """Who is calling the MCP endpoint, or the real server's auth error for this request."""
    auth = runtime.model.auth
    if auth is None or not runtime.auth.requires_auth("MCP *"):
        return None, None
    outcome = runtime.auth.authenticate(
        headers=dict(request.headers),
        query=dict(request.query_params),
        cookies=dict(request.cookies),
        now_ns=runtime.clocks.for_session(runtime.session_of(request)).now_ns(),
        required=True,
    )
    if outcome.failure is None:
        return outcome.identity, None
    ctx = runtime.new_context(request, None)
    response = runtime._auth_error(ctx, outcome.failure)  # noqa: SLF001 - same module family
    if response.status_code == 401:  # noqa: PLR2004
        path = runtime.model.mcp.transport.path if runtime.model.mcp else "/mcp"
        response.headers["www-authenticate"] = www_authenticate(request, path)
    return outcome.identity, response


def _add_spec_routes(app: FastAPI, runtime: FakeRuntime) -> None:
    spec_file = runtime.model.source.spec_file
    if not spec_file:
        return
    path = runtime.settings.data_dir / spec_file
    media = "application/yaml" if spec_file.endswith((".yaml", ".yml")) else "application/json"

    async def serve_spec(_request: Request) -> Response:
        if not path.exists():
            return Response(status_code=404)
        return Response(content=path.read_bytes(), media_type=media)

    for spec_path in runtime.model.routing.spec_paths:
        app.add_api_route(spec_path, serve_spec, methods=["GET"], include_in_schema=False)
