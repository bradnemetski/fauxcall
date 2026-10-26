"""The per-request context every handler (generic or custom) works with."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from starlette.requests import Request
from starlette.responses import Response

from fauxcall import jsonfmt, jsonpath
from fauxcall.clock import datetime_to_ns
from fauxcall.runtime import generators
from fauxcall.runtime.policies import apply_policies
from fauxcall.runtime.routing import path_value
from fauxcall.runtime.templating import (
    UNRESOLVED,
    Resolver,
    _Unresolved,
    chain,
    from_mapping,
    render,
    render_text,
)
from fauxcall.timefmt import TimeFormat
from fauxcall.world.references import apply_delete, missing_references, plan_delete

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable
    from datetime import tzinfo

    from starlette.requests import HTTPConnection

    from fauxcall.calendar import TradingCalendar
    from fauxcall.model.api import ApiModel
    from fauxcall.model.common import ZoneSpec
    from fauxcall.model.endpoints import EndpointModel, ResponseVariant
    from fauxcall.model.errors import ErrorCondition, ErrorTemplate
    from fauxcall.model.world import WorldRecord
    from fauxcall.runtime.app import FakeRuntime
    from fauxcall.runtime.auth import Identity
    from fauxcall.runtime.mcp_server import Channel
    from fauxcall.types import JsonValue
    from fauxcall.world import SessionWorld

DEFAULT_SESSION: Final = "default"
"""The session of requests that don't name one (``X-Fauxcall-Session``)."""

DEFAULT_STATUS: Final[dict[str, int]] = {
    "not_found": 404,
    "invalid_param": 400,
    "missing_param": 400,
    "invalid_body": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "expired": 401,
    "insufficient_scope": 403,
    "conflict": 409,
    "method_not_allowed": 405,
    "unknown_route": 404,
    "rate_limited": 429,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "missing_reference": 422,
    "still_referenced": 409,
    "custom": 400,
}
_NOW_FORMATS: Final[dict[str, TimeFormat]] = {
    "iso": TimeFormat(kind="iso"),
    "iso_ms": TimeFormat(kind="iso", fraction_digits=3),
    "iso_us": TimeFormat(kind="iso", fraction_digits=6),
    "iso_7": TimeFormat(kind="iso", fraction_digits=7),
    "iso_ns": TimeFormat(kind="iso", fraction_digits=9),
    "http_date": TimeFormat(kind="http_date"),
    "epoch": TimeFormat(kind="epoch"),
    "epoch_ms": TimeFormat(kind="epoch", epoch_unit="ms"),
    "date": TimeFormat(kind="date"),
}


class EarlyResponse(Exception):  # noqa: N818 - it carries a response, it is not an error
    """Raised inside the pipeline to answer immediately (auth failure, bad parameter …)."""

    def __init__(self, response: Response) -> None:
        super().__init__(response.status_code)
        self.response = response


@dataclass(slots=True)
class RequestContext:
    """Everything about the request being answered."""

    runtime: FakeRuntime
    request: HTTPConnection
    endpoint: EndpointModel | None
    session: str
    request_id: str
    now_ns: int
    rng: random.Random
    seq: int
    identity: Identity | None = None
    params: dict[str, JsonValue] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    fault: str | None = None
    body_bytes: bytes = b""
    json_body: JsonValue = None
    json_error: bool = False
    mcp_session: str | None = None
    """MCP: the call's ``Mcp-Session-Id`` (one per connected client), when the server issues one."""
    client_capabilities: dict[str, JsonValue] = field(default_factory=dict)
    """MCP: the capabilities the connected client declared in ``initialize`` (sampling …)."""
    notifications: list[JsonValue] = field(default_factory=list)
    """MCP: notifications sent while answering (``notify``), when nothing streams them."""
    on_notify: Callable[[JsonValue], None] | None = None
    """MCP: where ``notify`` sends a notification as it happens (an SSE response stream)."""
    channel: Channel | None = None
    """MCP: this client's channel for messages outside any request (``push``, ``background``)."""

    # -- shortcuts ---------------------------------------------------------------------------

    @property
    def model(self) -> ApiModel:
        """The API model."""
        return self.runtime.model

    @property
    def world(self) -> SessionWorld:
        """This session's view of the world."""
        return self.runtime.world.session(self.session)

    @property
    def state(self) -> dict[str, Any]:
        """This session's free-form handler state: kept across calls, cleared by reset.

        For state the world can't hold, like what an MCP tool created (``ctx.state["prices"]``).
        It lives in memory only.
        """
        return self.runtime.handler_state(self.session)

    async def pause(self, seconds: float) -> None:
        """Take as long as the real service would (a long job's steps …); skipped in fast mode.

        The time is the session's clock: advancing it (``POST /__fauxcall/clock``) ends the
        pause early. It is noted in ``runtime.pauses`` either way, so tests can check the timing
        in fast mode.
        """
        if self.runtime.settings.fast:
            self.runtime.pauses.append(seconds)
            return
        await self.runtime.wait(self.session, seconds)

    @property
    def cancelled(self) -> bool:
        """MCP: whether the client cancelled this call (``notifications/cancelled``).

        Its answer is dropped either way. Where the real server finishes cancelled calls
        (``on_cancel: finish``) the handler runs on; check this where the real tool checks its
        abort signal, and stop there.
        """
        message = self.json_body
        if self.channel is None or not isinstance(message, dict):
            return False
        return self.channel.was_cancelled(message.get("id"))

    def push(self, method: str, params: JsonValue = None) -> bool:
        """MCP: send this client a notification outside the call, on its open GET stream.

        Like a real server's ``notifications/resources/updated``. Returns whether a stream was
        open to take it (else it is dropped, as a real server would).
        """
        return self.channel.push(method, params) if self.channel is not None else False

    def background(self, work: Callable[[Channel], Awaitable[None]]) -> asyncio.Task[None] | None:
        """MCP: keep doing ``work(channel)`` after the call (periodic log messages …).

        It runs until it returns, is cancelled (keep the task, e.g. in ``ctx.state``, to stop
        it), or the client's session ends. ``channel.push(...)`` sends, ``await
        channel.pause(seconds)`` waits.
        """
        return self.channel.background(work) if self.channel is not None else None

    async def ask_client(
        self, method: str, params: JsonValue = None, *, within: float | None = None
    ) -> JsonValue:
        """MCP: ask the client something and wait for its answer, as real servers do mid-call.

        For ``sampling/createMessage``, ``elicitation/create`` or ``roots/list``. The request goes
        on this call's SSE stream, else on the client's GET stream. Returns the client's result;
        raises ``ClientError`` (an ``RpcError``: let it propagate to answer the call with it) when
        the client refuses, can't be reached, or doesn't answer in time.
        """
        if self.channel is None:
            from fauxcall.runtime.mcp_server import ClientError  # noqa: PLC0415

            raise ClientError(-32001, "Request timed out")
        if within is None:
            return await self.channel.ask(method, params, send=self.on_notify)
        return await self.channel.ask(method, params, within=within, send=self.on_notify)

    def notify(self, method: str, params: JsonValue = None) -> None:
        """MCP: send the client a notification while answering (``notifications/message`` …).

        With an SSE response it reaches the client at once, before the answer, like a real
        server's; otherwise it is sent with the answer, or dropped by a JSON-only transport.
        """
        message: dict[str, JsonValue] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if self.on_notify is not None:
            self.on_notify(message)
        else:
            self.notifications.append(message)

    def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        """MCP: a progress notification for this call, if the client asked for them.

        The client asks by sending a ``progressToken`` in the request's ``_meta``; without one
        nothing is sent, as on a real server.
        """
        request = self.json_body if isinstance(self.json_body, dict) else {}
        params = request.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        if token is None:
            return
        update: dict[str, JsonValue] = {"progressToken": token, "progress": progress}
        if total is not None:
            update["total"] = total
        if message is not None:
            update["message"] = message
        self.notify("notifications/progress", update)

    @property
    def endpoint_id(self) -> str:
        """The endpoint being served (empty for unknown routes)."""
        return self.endpoint.id if self.endpoint else ""

    def path_param(self, name: str) -> str | None:
        """A raw path parameter."""
        value = path_value(self.request.path_params, name)
        return None if value is None else str(value)

    def query(self, name: str) -> str | None:
        """The first raw value of a query parameter."""
        return self.request.query_params.get(name)

    def header(self, name: str) -> str | None:
        """A request header."""
        return self.request.headers.get(name)

    # -- PolicyEnvironment ----------------------------------------------------------------------

    def zone(self, spec: ZoneSpec, document: JsonValue) -> tzinfo:
        """Resolve ``UTC`` / IANA names, ``param:<name>`` and ``field:<path>`` (UTC on failure)."""
        name: JsonValue = spec
        if spec.startswith("param:"):
            key = spec.removeprefix("param:")
            name = self.params.get(key) or self.query(key) or self.path_param(key)
        elif spec.startswith("field:"):
            name = jsonpath.get_one(document, spec.removeprefix("field:"))
        if not isinstance(name, str) or not name or name == "calendar":
            return ZoneInfo("UTC")
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def calendar(self, name: str) -> TradingCalendar:
        """A calendar from the model."""
        return self.runtime.calendar(name)

    def next_counter(self, key: str, start: int, step: int) -> int:
        """Next value of a session counter."""
        return self.runtime.next_counter(self.session, f"{self.endpoint_id}:{key}", start, step)

    def series_value(self, name: str, mode: str) -> JsonValue:
        """Current value of a recorded series."""
        return self.runtime.series.value(name, self.now_ns, mode)

    def request_value(self, source: str) -> JsonValue:
        """``query.<n>``, ``path.<n>``, ``header.<n>``, ``param.<n>`` or ``body.<path>``."""
        kind, _, rest = source.partition(".")
        match kind:
            case "query":
                return self.query(rest)
            case "path":
                return self.path_param(rest)
            case "header":
                return self.header(rest)
            case "param":
                return self.params.get(rest)
            case "body":
                return jsonpath.get_one(self.json_body, rest or "$")
            case _:
                return None

    # -- bodies --------------------------------------------------------------------------------

    async def load_body(self) -> None:
        """Read the request body once (JSON bodies are parsed, keeping float spellings)."""
        if not isinstance(self.request, Request):
            return
        self.body_bytes = await self.request.body()
        content_type = self.request.headers.get("content-type", "")
        if self.body_bytes and "json" in content_type:
            try:
                self.json_body = jsonfmt.loads(self.body_bytes)
            except ValueError:
                self.json_error = True

    # -- templating ------------------------------------------------------------------------------

    def _generated(self, name: str, arg: str | None) -> JsonValue | _Unresolved:
        """Fresh values: ids, random strings, the clock."""
        rng = self.rng
        makers: dict[str, Callable[[], JsonValue]] = {
            "uuid": lambda: generators.uuid4(rng),
            "uuid4": lambda: generators.uuid4(rng),
            "uuid7": lambda: generators.uuid7(rng, self.now_ns),
            "traceparent": lambda: generators.traceparent(rng),
            "hex": lambda: generators.hex_string(rng, _length(arg, 32)),
            "digits": lambda: generators.digits(rng, _length(arg, 8)),
            "alnum": lambda: generators.alnum(rng, _length(arg, 16)),
            "now": lambda: _NOW_FORMATS.get(arg or "iso", _NOW_FORMATS["iso"]).render(self.now_ns),
        }
        maker = makers.get(name)
        return UNRESOLVED if maker is None else maker()

    def _from_request(self, name: str) -> JsonValue | _Unresolved:
        """Values taken from the request being answered."""
        simple: dict[str, JsonValue] = {
            "method": str(self.request.scope.get("method", "GET")),
            "path": self.request.url.path,
            "url": str(self.request.url),
            "query": self.request.url.query,
            "request_id": self.request_id,
            "identity": self.identity.name if self.identity else None,
        }
        if name in simple:
            return simple[name]
        prefix, _, rest = name.partition(".")
        match prefix:
            case "param":
                raw = self.path_param(rest) or self.query(rest)
                return raw if raw is not None else self.params.get(rest)
            case "header":
                return self.header(rest)
            case "body":
                return jsonpath.get_one(self.json_body, rest)
            case _:
                return UNRESOLVED

    def resolver(self, **extra: JsonValue) -> Resolver:
        """Placeholder resolver for templates rendered in this request (see model.errors)."""

        def builtin(name: str, arg: str | None) -> JsonValue | _Unresolved:
            generated = self._generated(name, arg)
            return generated if not isinstance(generated, _Unresolved) else self._from_request(name)

        return chain(from_mapping(dict(extra)), builtin)

    # -- responses -------------------------------------------------------------------------------

    def variant(self, status: int) -> ResponseVariant | None:
        """The endpoint's response variant for ``status``."""
        if self.endpoint is None:
            return None
        return next((v for v in self.endpoint.responses if v.status == status), None)

    def _headers(
        self, status: int, extra: dict[str, str] | None, values: dict[str, JsonValue]
    ) -> dict[str, str]:
        resolver = self.resolver(**values)
        headers: dict[str, str] = {}
        for name, value in self.model.default_headers.items():
            headers[name] = render_text(value, resolver)
        variant = self.variant(status)
        if variant is not None:
            for name, rule in variant.headers.items():
                headers[name] = render_text(rule.value, resolver)
        headers.update(self.extra_headers)
        headers.update(extra or {})
        return headers

    def respond(
        self,
        body: JsonValue,
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
        values: dict[str, JsonValue] | None = None,
    ) -> Response:
        """A JSON response spelled exactly like the real API's."""
        code = status or (self.endpoint.success_status if self.endpoint else 200)
        variant = self.variant(code)
        media = content_type or (variant.content_type if variant else None) or "application/json"
        merged = self._headers(code, headers, values or {})
        merged.setdefault("content-type", media)
        text = jsonfmt.dumps(body, self.model.json_style)
        return Response(content=text.encode("utf-8"), status_code=code, headers=merged)

    def respond_bytes(
        self,
        content: bytes,
        *,
        status: int | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """A raw (binary or text) response."""
        code = status or (self.endpoint.success_status if self.endpoint else 200)
        merged = self._headers(code, headers, {})
        if content_type:
            merged.setdefault("content-type", content_type)
        return Response(content=content, status_code=code, headers=merged)

    def empty(self, status: int = 204, headers: dict[str, str] | None = None) -> Response:
        """A body-less response."""
        return Response(status_code=status, headers=self._headers(status, headers, {}))

    def render_record(self, record: WorldRecord) -> JsonValue:
        """A record as the endpoint returns it: projection, then record-scope policies."""
        value = record.record
        endpoint = self.endpoint
        if endpoint is not None and endpoint.shape.projection and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k in endpoint.shape.projection}
        anchor = datetime_to_ns(record.recorded_at) if record.recorded_at else None
        policies = endpoint.fields if endpoint is not None else []
        return apply_policies(value, policies, self, scope="record", anchor_ns=anchor)

    def render_body(self, body: JsonValue, *, anchor_ns: int | None = None) -> JsonValue:
        """Apply body-scope policies to a whole response body."""
        policies = self.endpoint.fields if self.endpoint is not None else []
        return apply_policies(body, policies, self, scope="body", anchor_ns=anchor_ns)

    # -- errors ---------------------------------------------------------------------------------

    def _template_for(self, condition: ErrorCondition, param: str | None) -> ErrorTemplate | None:
        if self.endpoint is not None:
            case = self.endpoint.error_case(condition, param)
            if case is not None:
                return self.model.error_template(case.template)
        return next((t for t in self.model.errors if t.name == condition), None)

    def refuse_missing(self, collection: str, record: JsonValue) -> Response | None:
        """The real API's refusal of a write pointing at a target that doesn't exist, or ``None``.

        For relations from ``collection`` whose ``on_missing`` is ``refuse``: the endpoint's
        ``missing_reference`` error. Custom handlers call it before storing a record::

            if (refused := ctx.refuse_missing("books", record)) is not None:
                return refused
        """
        missing = missing_references(self.model, self.world, collection, record)
        if not missing:
            return None
        relation, value = missing[0]
        return self.error("missing_reference", param=relation.from_field, value=value)

    def delete(self, collection: str, key: JsonValue) -> Response | None:
        """Delete a record as the real API does; ``None`` once done, else the refusal.

        Relations' ``on_delete`` apply: ``restrict`` refuses with the endpoint's
        ``still_referenced`` error while records point here (nothing changes), ``cascade``
        deletes them too, ``set_null`` clears their references. A record that doesn't exist is
        left alone (answer the endpoint's ``not_found`` before calling this).
        """
        record = self.world.get(collection, key)
        if record is None:
            return None
        plan = plan_delete(self.model, self.world, collection, record)
        if plan.blocked_by is not None:
            relation, _ = plan.blocked_by
            return self.error("still_referenced", param=relation.from_field, value=key)
        apply_delete(self.world, plan)
        for deleted_from, deleted in plan.deletes:
            self.runtime.drop_blob(self, deleted_from, deleted.key)
        return None

    def error(
        self,
        condition: ErrorCondition,
        *,
        param: str | None = None,
        value: JsonValue = None,
        template: str | None = None,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        values: dict[str, JsonValue] | None = None,
    ) -> Response:
        """Respond with the real API's error for ``condition`` (or a sensible generic one)."""
        chosen = (
            self.model.error_template(template)
            if template
            else self._template_for(condition, param)
        )
        extra: dict[str, JsonValue] = {"value": value, "param": param, **(values or {})}
        if chosen is None:
            code = status or DEFAULT_STATUS[condition]
            body: dict[str, JsonValue] = {
                "error": condition,
                "message": condition.replace("_", " "),
            }
            if param:
                body["param"] = param
            return Response(
                content=jsonfmt.dumps(body).encode(),
                status_code=code,
                headers={
                    "content-type": "application/json",
                    **self.extra_headers,
                    **(headers or {}),
                },
            )
        code = status or chosen.status
        extra["status"] = code
        resolver = self.resolver(**extra)
        merged = {name: render_text(v, resolver) for name, v in chosen.headers.items()}
        merged.update(self.extra_headers)
        merged.update(headers or {})
        if chosen.body_text is not None:
            content = render_text(chosen.body_text, resolver).encode()
            if chosen.content_type:
                merged.setdefault("content-type", chosen.content_type)
            return Response(content=content, status_code=code, headers=merged)
        if chosen.body_json is None:
            if chosen.content_type:
                merged.setdefault("content-type", chosen.content_type)
            return Response(status_code=code, headers=merged)
        merged.setdefault("content-type", chosen.content_type or "application/json")
        text = jsonfmt.dumps(render(chosen.body_json, resolver), self.model.json_style)
        return Response(content=text.encode(), status_code=code, headers=merged)

    def fail(self, condition: ErrorCondition, **kwargs: JsonValue) -> EarlyResponse:
        """An :class:`EarlyResponse` carrying :meth:`error` (``raise ctx.fail("not_found")``)."""
        param = kwargs.pop("param", None)
        value = kwargs.pop("value", None)
        return EarlyResponse(
            self.error(
                condition, param=None if param is None else str(param), value=value, values=kwargs
            )
        )


def _length(arg: str | None, default: int) -> int:
    """A ``{{hex:12}}``-style length argument (the default when missing or not a number)."""
    text = (arg or "").strip()
    return int(text) if text.isascii() and text.isdigit() else default


def request_rng(seed: int, session: str, seq: int) -> random.Random:
    """The deterministic generator for one request."""
    return random.Random(f"{seed}:{session}:{seq}")
