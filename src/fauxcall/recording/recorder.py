"""Execute recording plans against the real API and store every exchange.

Safety first: read-only calls run freely; a mutating call only runs when the single up-front
approval covers it (``data/approvals.json``), and every call counts against a budget. Requests are
paced, credentials never reach disk, and personal data is pseudonymized before anything is written.

Privacy runs in an order that keeps pseudonyms consistent: response bodies and streamed payloads
first (so personal values they reveal are known), then the request body, then the URL, path, query
and headers, which are scrubbed of every value seen so far. JSON payloads inside SSE events and
WebSocket messages get the same JSON treatment as bodies. Whatever the privacy filter does, the
target's own credentials are scrubbed from the finished exchange before it is written.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx2

from fauxcall import jsonfmt, jsonpath
from fauxcall.jsonfmt import infer_style
from fauxcall.logs import get_logger
from fauxcall.mcpwire.protocol import SseDecoder
from fauxcall.model.recording import (
    Body,
    CapturedRequest,
    CapturedResponse,
    Exchange,
    MultipartPart,
    StreamEvent,
)
from fauxcall.privacy import MIN_SECRET_LENGTH, scrub_text
from fauxcall.runtime.templating import from_mapping, render, render_text
from fauxcall.storage import model_to_json, write_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from httpx2.websockets import AsyncWebSocketSession

    from fauxcall.model.common import Purpose
    from fauxcall.model.plan import (
        Approvals,
        PlannedCall,
        PlannedFile,
        PlannedRequest,
        PollSpec,
        RecordingPlan,
        StreamSpec,
        WsFrame,
    )
    from fauxcall.recording.store import RecordingStore
    from fauxcall.runtime.templating import Resolver
    from fauxcall.types import JsonValue

log = get_logger(__name__)

INVALID_CREDENTIAL: Final = "fauxcall-invalid-credential"
_TEXT_TYPES: Final = ("text/", "xml", "html", "javascript", "x-www-form-urlencoded", "csv", "yaml")
_PATH_SAFE: Final = "/:@!$&'()*+,;="
"""Characters left as they are when a path parameter is put into the path (RFC 3986 ``pchar``)."""
_AUTH_HEADERS: Final = frozenset({"authorization", "proxy-authorization"})
_SCHEMES: Final = frozenset(
    {
        "apikey",
        "api-key",
        "basic",
        "bearer",
        "bot",
        "digest",
        "dpop",
        "gnap",
        "jwt",
        "key",
        "oauth",
        "ssws",
        "token",
    }
)
"""Credential schemes recognized in any header (``Authorization`` accepts any scheme token)."""
_SCHEME_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]*")


class PrivacyFilter(Protocol):
    """What the recorder needs from the privacy layer (see :mod:`fauxcall.privacy`)."""

    def request(
        self,
        *,
        url: str,
        path: str,
        path_params: Mapping[str, str],
        query: Sequence[tuple[str, str]],
        headers: Sequence[tuple[str, str]],
        json_body: JsonValue,
    ) -> tuple[
        str, str, dict[str, str], list[tuple[str, str]], list[tuple[str, str]], JsonValue, list[str]
    ]:
        """Redact/pseudonymize request parts.

        Returns the URL, path, path params, query, headers, JSON body and notes.
        """
        ...

    def response_headers(
        self, headers: Sequence[tuple[str, str]]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Redact response headers."""
        ...

    def json(self, value: JsonValue) -> tuple[JsonValue, list[str], list[str]]:
        """Redact + pseudonymize a JSON body. Returns value, redacted paths, pseudonymized paths."""
        ...

    def text(self, value: str) -> str:
        """Scrub secrets and known personal values from free text."""
        ...


class NoPrivacy:
    """Pass-through filter (tests, or APIs with nothing sensitive).

    The target's own credentials are still scrubbed by the recorder before anything is written.
    """

    def request(
        self,
        *,
        url: str,
        path: str,
        path_params: Mapping[str, str],
        query: Sequence[tuple[str, str]],
        headers: Sequence[tuple[str, str]],
        json_body: JsonValue,
    ) -> tuple[
        str, str, dict[str, str], list[tuple[str, str]], list[tuple[str, str]], JsonValue, list[str]
    ]:
        """Return everything unchanged."""
        return url, path, dict(path_params), list(query), list(headers), json_body, []

    def response_headers(
        self, headers: Sequence[tuple[str, str]]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Return headers unchanged."""
        return list(headers), []

    def json(self, value: JsonValue) -> tuple[JsonValue, list[str], list[str]]:
        """Return the value unchanged."""
        return value, [], []

    def text(self, value: str) -> str:
        """Return the text unchanged."""
        return value


@dataclass(slots=True)
class RealTarget:
    """Where and how to reach the real API."""

    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
    user_agent: str | None = None


@dataclass(slots=True)
class CallOutcome:
    """What happened to one planned call."""

    call: str
    endpoint: str
    exchanges: list[str] = field(default_factory=list)
    statuses: list[int] = field(default_factory=list)
    skipped: str | None = None
    error: str | None = None


@dataclass(slots=True)
class RunReport:
    """Summary of a plan run (printed for the agent)."""

    plan: str
    outcomes: list[CallOutcome] = field(default_factory=list)
    calls_made: int = 0
    budget_exhausted: bool = False

    def summary(self) -> str:
        """A compact markdown table."""
        lines = [
            f"Plan `{self.plan}`: {self.calls_made} real calls"
            + (" — **budget exhausted**" if self.budget_exhausted else ""),
            "",
            "| call | endpoint | statuses | exchanges | note |",
            "|---|---|---|---|---|",
        ]
        for outcome in self.outcomes:
            note = outcome.skipped or outcome.error or ""
            statuses = ",".join(str(s) for s in outcome.statuses)
            exchanges = (
                f"{outcome.exchanges[0]}..{outcome.exchanges[-1]}" if outcome.exchanges else "-"
            )
            lines.append(
                f"| {outcome.call} | {outcome.endpoint} | {statuses} | {exchanges} | {note} |"
            )
        return "\n".join(lines)


class BudgetExhaustedError(Exception):
    """The recording budget ran out."""


def classify_body(content_type: str | None, raw: bytes) -> tuple[Body, bytes | None]:
    """Turn raw bytes into a :class:`Body` (binary content is returned separately for a sidecar).

    JSON keeps its exact spelling (``105.50`` stays ``105.50``): the value is stored parsed with
    the inferred :class:`~fauxcall.jsonfmt.JsonStyle`, plus the original text when that style
    cannot reproduce it byte for byte.
    """
    media = (content_type or "").lower()
    if not raw:
        return Body(kind="empty", content_type=content_type), None
    looks_json = "json" in media or (not media and raw.lstrip()[:1] in {b"{", b"["})
    if looks_json:
        try:
            text = raw.decode("utf-8")
            value = jsonfmt.loads(text)
        except (UnicodeDecodeError, ValueError):
            pass
        else:
            style = infer_style([text])
            exact = jsonfmt.dumps(value, style) == text
            return (
                Body(
                    kind="json",
                    content_type=content_type,
                    json_value=value,
                    style=style,
                    text=None if exact else text,
                    size=len(raw),
                ),
                None,
            )
    if any(marker in media for marker in _TEXT_TYPES) or looks_json:
        try:
            return Body(
                kind="text", content_type=content_type, text=raw.decode("utf-8"), size=len(raw)
            ), None
        except UnicodeDecodeError:
            pass
    digest = hashlib.sha256(raw).hexdigest()
    return Body(kind="binary", content_type=content_type, sha256=digest, size=len(raw)), raw


def credential_values(
    headers: Mapping[str, str], query: Mapping[str, str] | None = None
) -> list[str]:
    """The secrets inside configured credentials, for scrubbing.

    A header value with an auth scheme yields the credential after it (``Bearer abc123`` ->
    ``abc123``, so scrubbed text keeps ``Bearer [REDACTED]``); anything else yields the whole value.
    Values shorter than :data:`~fauxcall.privacy.MIN_SECRET_LENGTH` are left out (scrubbing them
    would mangle unrelated text).
    """
    candidates = [_credential_of(name, value) for name, value in headers.items()]
    candidates += [value.strip() for value in (query or {}).values()]
    found: list[str] = []
    for candidate in candidates:
        if len(candidate) >= MIN_SECRET_LENGTH and candidate not in found:
            found.append(candidate)
    return found


def _credential_of(header: str, value: str) -> str:
    """The secret part of a configured header value (after its auth scheme, if any)."""
    whole = value.strip()
    if _scheme_of(whole, header) is None:
        return whole
    secret = whole.partition(" ")[2].strip()
    return secret if len(secret) >= MIN_SECRET_LENGTH else whole


def private_text(
    privacy: PrivacyFilter,
    data: str,
    where: str,
    redacted: list[str],
    pseudonymized: list[str],
) -> str:
    """Free text from a stream (an SSE event, a WebSocket message) made safe for disk.

    A JSON payload gets the full JSON treatment (secret-named fields redacted, personal data
    pseudonymized, notes recorded under ``where``) and keeps its spelling when nothing changed;
    anything else is scrubbed as text.
    """
    if data.lstrip()[:1] in {"{", "["}:
        try:
            value = jsonfmt.loads(data)
        except ValueError:
            pass
        else:
            safe, red, pseudo = privacy.json(value)
            redacted += [f"{where}:{p}" for p in red]
            pseudonymized += [f"{where}:{p}" for p in pseudo]
            if red or pseudo or safe != value:
                return jsonfmt.dumps(safe, infer_style([data]))
    return privacy.text(data)


def _scrub_tree(node: JsonValue, secrets: Sequence[str]) -> JsonValue:
    if isinstance(node, str):
        return scrub_text(node, secrets)
    if isinstance(node, list):
        return [_scrub_tree(item, secrets) for item in node]
    if isinstance(node, dict):
        return {scrub_text(key, secrets): _scrub_tree(item, secrets) for key, item in node.items()}
    return node


def scrub_exchange(exchange: Exchange, secrets: Sequence[str]) -> Exchange:
    """``exchange`` with every occurrence of ``secrets`` replaced by ``[REDACTED]``.

    The last line of defense before disk: it holds whatever the privacy filter did or missed.
    """
    if not secrets:
        return exchange
    tree = model_to_json(exchange)
    cleaned = _scrub_tree(tree, secrets)
    return exchange if cleaned == tree else Exchange.model_validate(cleaned)


def _header(headers: dict[str, str] | Sequence[tuple[str, str]], name: str) -> str | None:
    """A header value by case-insensitive name (first one wins)."""
    pairs = headers.items() if isinstance(headers, dict) else headers
    lowered = name.lower()
    return next((value for key, value in pairs if key.lower() == lowered), None)


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    """Set a header, replacing any spelling of the same name."""
    for key in [key for key in headers if key.lower() == name.lower()]:
        del headers[key]
    headers[name] = value


def _scheme_of(value: str, header: str) -> str | None:
    """The auth scheme in front of a credential (``Bearer`` in ``Bearer abc``), if any.

    Known schemes count in any header; ``Authorization``/``Proxy-Authorization`` accept any
    scheme token (``Bot``, ``GenieKey`` …).
    """
    scheme, space, secret = value.strip().partition(" ")
    if not space or not secret.strip():
        return None
    known = scheme.casefold() in _SCHEMES
    if known or (header.lower() in _AUTH_HEADERS and _SCHEME_RE.fullmatch(scheme)):
        return scheme
    return None


def _keep_scheme(original: str, replacement: str, *, header: str = "") -> str:
    """Keep an auth scheme prefix when swapping a credential (``Bearer x`` -> ``Bearer y``)."""
    if replacement == original:
        return replacement
    scheme = _scheme_of(original, header)
    return replacement if scheme is None else f"{scheme} {replacement}"


def _state_text(state: JsonValue) -> str:
    """A JSON value as text: strings verbatim, anything else in JSON spelling (``true``, ``3``)."""
    return state if isinstance(state, str) else jsonfmt.dumps(state)


def _tags(origin: _Origin) -> list[str]:
    """The call's tags, plus ``auth:<mode>`` for a probe sent without or with a bad credential.

    After redaction such a probe looks like a normal call; the tag lets replays reproduce it.
    """
    tags = list(origin.tags)
    if origin.request.auth != "default" and f"auth:{origin.request.auth}" not in tags:
        tags.append(f"auth:{origin.request.auth}")
    return tags


def _note(outcome: CallOutcome, exchange: Exchange) -> None:
    """Add an exchange (and its status) to a call's outcome."""
    outcome.exchanges.append(exchange.id)
    outcome.statuses += [exchange.response.status] if exchange.response is not None else []


def _content_length(headers: Sequence[tuple[str, str]]) -> int:
    value = _header(headers, "content-length")
    return int(value) if value is not None and value.isdigit() else 0


def _ws_url(url: str) -> str:
    """``http(s)://`` -> ``ws(s)://``."""
    if url.startswith("https://"):
        return "wss://" + url.removeprefix("https://")
    if url.startswith("http://"):
        return "ws://" + url.removeprefix("http://")
    return url


@dataclass(slots=True)
class _Built:
    """A planned request with its templates rendered and credentials attached."""

    url: str
    path: str
    params: dict[str, str]
    query: list[tuple[str, str]]
    headers: dict[str, str]
    json_body: JsonValue


@dataclass(slots=True)
class _Payload:
    """What goes on the wire as the request body."""

    kind: Literal["empty", "json", "text", "form", "multipart"] = "empty"
    content: bytes | None = None
    text: str | None = None
    form: dict[str, str] | None = None
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None
    content_type: str | None = None


@dataclass(slots=True)
class _Received:
    """The response side of an exchange, before privacy."""

    status: int
    reason: str | None = None
    http_version: str | None = None
    sent_headers: list[tuple[str, str]] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: Body | None = None
    binary: bytes | None = None
    events: list[StreamEvent] = field(default_factory=list)
    started_ns: int = 0
    elapsed_ms: float = 0.0
    ttfb_ms: float | None = None
    note: str | None = None

    @classmethod
    def of(cls, response: httpx2.Response, started: float) -> _Received:
        """Status line and headers of ``response`` (``started``: ``perf_counter`` at send)."""
        return cls(
            status=response.status_code,
            reason=response.reason_phrase or None,
            http_version=response.http_version,
            sent_headers=list(response.request.headers.multi_items()),
            headers=list(response.headers.multi_items()),
            ttfb_ms=(time.perf_counter() - started) * 1000,
        )

    def classify(self, raw: bytes) -> Body:
        """Store ``raw`` as the response body (binary content goes to :attr:`binary`)."""
        body, self.binary = classify_body(_header(self.headers, "content-type"), raw)
        self.body = body
        return body


@dataclass(slots=True)
class _Origin:
    """The plan call an exchange belongs to."""

    endpoint: str
    purpose: Purpose
    call_id: str
    tags: list[str]
    request: PlannedRequest


@dataclass(slots=True)
class _Draft:
    """Bookkeeping while one exchange is assembled."""

    exchange_id: str
    sidecars: dict[str, bytes] = field(default_factory=dict)
    request_notes: list[str] = field(default_factory=list)
    redacted: list[str] = field(default_factory=list)
    pseudonymized: list[str] = field(default_factory=list)


async def _read_events(
    response: httpx2.Response, spec: StreamSpec, started: float
) -> list[StreamEvent]:
    """Record a streamed body as SSE events or raw chunks, for ``spec.duration_s`` at most."""
    events: list[StreamEvent] = []
    decoder = SseDecoder() if spec.kind == "sse" else None
    remaining = max(0.0, started + spec.duration_s - time.perf_counter())
    with contextlib.suppress(TimeoutError, httpx2.ReadTimeout):
        async with asyncio.timeout(remaining):
            async for chunk in response.aiter_text():  # never yields empty text
                stamp = round((time.perf_counter() - started) * 1000, 3)
                if decoder is None:
                    events.append(StreamEvent(t_ms=stamp, kind="chunk", data=chunk))
                else:
                    events += [
                        StreamEvent(
                            t_ms=stamp,
                            kind="sse",
                            event=event.event,
                            id=event.id,
                            data=event.data,
                            retry=event.retry,
                        )
                        for event in decoder.feed(chunk)
                    ]
                if len(events) >= spec.max_events:
                    break
    return events[: spec.max_events]


class Recorder:
    """Runs plans and writes exchanges."""

    def __init__(  # noqa: PLR0913 - mirrors the CLI's recording options, plus injectable clocks
        self,
        store: RecordingStore,
        target: RealTarget,
        http: httpx2.AsyncClient,
        approvals: Approvals,
        *,
        approvals_path: Path | None = None,
        privacy: PrivacyFilter | None = None,
        rps: float = 2.0,
        max_calls: int = 2000,
        files_root: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.target = target
        self.http = http
        self.approvals = approvals
        self.approvals_path = approvals_path
        self.privacy: PrivacyFilter = privacy or NoPrivacy()
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self.max_calls = max_calls
        self.files_root = files_root
        self._sleep = sleep
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self._secrets = credential_values(target.headers, target.query)
        self.calls_made = 0
        self._last_sent: float | None = None

    # -- plan execution --------------------------------------------------------------------------

    async def run(self, plan: RecordingPlan) -> RunReport:
        """Execute every call of ``plan`` in order.

        Approval usage is written to ``approvals_path`` even when the run fails half-way, so a
        budget can never be spent twice.
        """
        report = RunReport(plan=plan.name)
        variables: dict[str, JsonValue] = {}
        try:
            for position, call in enumerate(plan.calls):
                outcome = CallOutcome(call=call.id, endpoint=call.endpoint)
                report.outcomes.append(outcome)
                if call.is_mutating and self.approvals.find(call) is None:
                    outcome.skipped = "needs approval (mutating call not covered by approvals.json)"
                    continue
                try:
                    await self._execute(call, variables, outcome)
                except BudgetExhaustedError:
                    outcome.skipped = "budget exhausted"
                    report.budget_exhausted = True
                    report.outcomes += [
                        CallOutcome(
                            call=rest.id, endpoint=rest.endpoint, skipped="budget exhausted"
                        )
                        for rest in plan.calls[position + 1 :]
                    ]
                    break
                except (httpx2.HTTPError, OSError, ValueError) as exc:
                    outcome.error = f"{type(exc).__name__}: {exc}"[:300]
                    log.warning("recording_call_failed", call=call.id, error=outcome.error)
        finally:
            report.calls_made = self.calls_made
            if self.approvals_path is not None:
                write_model(self.approvals_path, self.approvals)
        return report

    async def _execute(
        self, call: PlannedCall, variables: dict[str, JsonValue], outcome: CallOutcome
    ) -> None:
        for index in range(call.repeat):
            if index and call.interval_ms:
                await self._sleep(call.interval_ms / 1000)
            approval = None
            if call.is_mutating:
                approval = self.approvals.find(call)
                if approval is None:
                    outcome.skipped = f"approval budget used up after {index} call(s)"
                    return
            await self._pace()  # raises before an approval is spent when the budget is gone
            if approval is not None:
                self.approvals.consume(approval)
            raw = await self._once(call, variables, outcome)
            for name, path in call.capture.items():
                captured = jsonpath.get_one(raw, path)
                if captured is not None:
                    variables[name] = captured
        if call.poll is not None:
            await self._poll(call, call.poll, variables, outcome)

    async def _once(
        self, call: PlannedCall, variables: Mapping[str, JsonValue], outcome: CallOutcome
    ) -> JsonValue:
        """Make one planned request; returns the response's JSON (real values, for captures)."""
        if call.stream is not None:
            exchange, raw = await self._stream(call, call.stream, variables)
        else:
            origin = _Origin(call.endpoint, call.purpose, call.id, list(call.tags), call.request)
            exchange, raw = await self._send(origin, variables)
        _note(outcome, exchange)
        return raw

    async def _poll(
        self,
        call: PlannedCall,
        poll: PollSpec,
        variables: dict[str, JsonValue],
        outcome: CallOutcome,
    ) -> None:
        """Poll until a terminal state. Polls repeat without approval, so they must be safe."""
        if not poll.request.method.is_safe:
            outcome.error = (
                f"poll requests must be read-only; {poll.request.method.value} is not polled"
            )
            return
        deadline = self._monotonic() + poll.timeout_s
        origin = _Origin(
            poll.endpoint, "job_poll", call.id, [*call.tags, f"poll:{call.id}"], poll.request
        )
        while True:
            await self._pace()
            exchange, raw = await self._send(origin, variables)
            _note(outcome, exchange)
            state = jsonpath.get_one(raw, poll.state_path)
            if state is not None and _state_text(state) in poll.terminal:
                return
            if self._monotonic() >= deadline:
                outcome.error = f"poll timed out after {poll.timeout_s}s (last state {state!r})"
                return
            await self._sleep(poll.interval_ms / 1000)

    # -- sending ---------------------------------------------------------------------------------

    async def _pace(self) -> None:
        if self.calls_made >= self.max_calls:
            raise BudgetExhaustedError
        if self._last_sent is not None:
            wait = self._last_sent + self.min_interval - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
        self._last_sent = self._monotonic()
        self.calls_made += 1

    def _build(self, request: PlannedRequest, variables: Mapping[str, JsonValue]) -> _Built:
        resolver = from_mapping(dict(variables))
        params = {k: render_text(v, resolver) for k, v in request.path_params.items()}
        path = render_text(request.path, resolver)
        for name, value in params.items():
            path = path.replace("{" + name + "}", quote(value, safe=_PATH_SAFE))
        path, _, inline = path.partition("?")
        if not path.startswith("/"):
            path = "/" + path
        query = [
            *parse_qsl(inline, keep_blank_values=True),
            *((k, render_text(v, resolver)) for k, v in request.query),
        ]
        headers = {k: render_text(v, resolver) for k, v in request.headers.items()}
        if self.target.user_agent and _header(headers, "user-agent") is None:
            headers["user-agent"] = self.target.user_agent
        if request.auth != "none":
            invalid = request.auth == "invalid"
            for name, value in self.target.headers.items():
                sent = _keep_scheme(value, INVALID_CREDENTIAL, header=name) if invalid else value
                _set_header(headers, name, sent)
            query += [
                (k, INVALID_CREDENTIAL if invalid else v) for k, v in self.target.query.items()
            ]
        body = render(request.json_body, resolver) if request.json_body is not None else None
        url = self.target.base_url.rstrip("/") + path
        if query:
            url = f"{url}?{urlencode(query)}"
        return _Built(url, path, params, query, headers, body)

    def _payload(self, request: PlannedRequest, built: _Built, resolver: Resolver) -> _Payload:
        """The request body, and its content type unless the plan set one."""
        if built.json_body is not None:
            payload = _Payload(
                kind="json",
                content=jsonfmt.dumps(built.json_body).encode(),
                content_type=request.content_type or "application/json",
            )
        elif request.text_body is not None:
            text = render_text(request.text_body, resolver)
            payload = _Payload(
                kind="text", content=text.encode(), text=text, content_type=request.content_type
            )
        else:
            form = {k: render_text(v, resolver) for k, v in (request.form or {}).items()}
            if request.files:
                files = [
                    (f.field, (f.filename, self._file_bytes(f), f.content_type))
                    for f in request.files
                ]
                payload = _Payload(kind="multipart", form=form or None, files=files)
            else:
                payload = _Payload(kind="form" if form else "empty", form=form or None)
        if payload.content_type and _header(built.headers, "content-type") is None:
            built.headers["content-type"] = payload.content_type
        return payload

    def _file_bytes(self, planned: PlannedFile) -> bytes:
        if planned.path is not None:
            if self.files_root is None:
                raise ValueError("planned files with a path need files_root")
            root = self.files_root.resolve()
            target = (root / planned.path).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"planned file {planned.path!r} is outside {root}")
            return target.read_bytes()
        if planned.text is not None:
            return planned.text.encode()
        try:
            return base64.b64decode(planned.base64 or "", validate=True)
        except binascii.Error as exc:
            raise ValueError(f"planned file {planned.filename!r} has invalid base64") from exc

    async def _send(
        self, origin: _Origin, variables: Mapping[str, JsonValue]
    ) -> tuple[Exchange, JsonValue]:
        built = self._build(origin.request, variables)
        payload = self._payload(origin.request, built, from_mapping(dict(variables)))
        draft = _Draft(self.store.next_id())
        started_ns = self._clock_ns()
        started = time.perf_counter()
        async with self.http.stream(
            origin.request.method.value,
            built.url,
            headers=built.headers,
            content=payload.content,
            files=payload.files,
            data=payload.form,
            timeout=self.target.timeout_s,
            follow_redirects=False,
        ) as response:
            received = _Received.of(response, started)
            raw_body = await response.aread()
        received.started_ns = started_ns
        received.elapsed_ms = (time.perf_counter() - started) * 1000
        raw = received.classify(raw_body).parsed()
        exchange = self._exchange(draft, origin, built, payload, received)
        self.store.save(exchange, draft.sidecars)
        return exchange, raw

    # -- assembling an exchange (privacy first) --------------------------------------------------

    def _exchange(
        self, draft: _Draft, origin: _Origin, built: _Built, payload: _Payload, received: _Received
    ) -> Exchange:
        body = Body()
        if received.body is not None:
            body = self._private_body(draft, received.body)
            if received.binary is not None:
                name = self.store.sidecar_name(draft.exchange_id, "response")
                draft.sidecars[name] = received.binary
                body = body.model_copy(update={"file": name})
        events = [
            event
            if event.data is None
            else event.model_copy(
                update={"data": self._private_text(draft, event.data, f"stream[{index}]")}
            )
            for index, event in enumerate(received.events)
        ]
        request = self._request_record(draft, origin.request, built, payload, received.sent_headers)
        headers, header_notes = self.privacy.response_headers(received.headers)
        exchange = Exchange(
            id=draft.exchange_id,
            endpoint=origin.endpoint,
            purpose=origin.purpose,
            plan_call=origin.call_id,
            started_at=datetime.fromtimestamp(received.started_ns / 1e9, UTC),
            started_ns=received.started_ns,
            elapsed_ms=round(received.elapsed_ms, 3),
            ttfb_ms=None if received.ttfb_ms is None else round(received.ttfb_ms, 3),
            request=request,
            response=CapturedResponse(
                status=received.status,
                reason=received.reason,
                http_version=received.http_version,
                headers=headers,
                body=body,
            ),
            stream=events,
            tags=_tags(origin),
            note=received.note,
            redacted=[
                *draft.request_notes,
                *draft.redacted,
                *(f"response.header:{name}" for name in header_notes),
            ],
            pseudonymized=draft.pseudonymized,
        )
        return scrub_exchange(exchange, self._secrets)

    def _private_body(self, draft: _Draft, body: Body) -> Body:
        if body.kind == "json":
            value, red, pseudo = self.privacy.json(body.json_value)
            draft.redacted += [f"response:{p}" for p in red]
            draft.pseudonymized += [f"response:{p}" for p in pseudo]
            changed = bool(red or pseudo) or value != body.json_value
            # The exact original spelling can't be kept once values changed (it would leak them).
            text = None if body.text is None or changed else self.privacy.text(body.text)
            return body.model_copy(update={"json_value": value, "text": text})
        if body.kind == "text" and body.text is not None:
            return body.model_copy(update={"text": self.privacy.text(body.text)})
        return body

    def _private_text(self, draft: _Draft, data: str, where: str) -> str:
        return private_text(self.privacy, data, where, draft.redacted, draft.pseudonymized)

    def _request_record(
        self,
        draft: _Draft,
        request: PlannedRequest,
        built: _Built,
        payload: _Payload,
        sent_headers: Sequence[tuple[str, str]],
    ) -> CapturedRequest:
        url, path, params, query, headers, safe_json, notes = self.privacy.request(
            url=built.url,
            path=built.path,
            path_params=built.params,
            query=built.query,
            headers=sent_headers,
            json_body=built.json_body,
        )
        draft.request_notes += notes
        return CapturedRequest(
            method=request.method,
            url=url,
            path=path,
            path_params=params,
            query=query,
            headers=headers,
            body=self._request_body(draft, request, payload, safe_json, sent_headers),
        )

    def _request_body(
        self,
        draft: _Draft,
        request: PlannedRequest,
        payload: _Payload,
        safe_json: JsonValue,
        sent_headers: Sequence[tuple[str, str]],
    ) -> Body:
        content_type = _header(sent_headers, "content-type")
        size = _content_length(sent_headers)
        if payload.kind == "json":
            return Body(kind="json", content_type=content_type, json_value=safe_json, size=size)
        if payload.kind == "text" and payload.text is not None:
            text = self.privacy.text(payload.text)
            return Body(kind="text", content_type=content_type, text=text, size=size)
        if payload.kind == "empty":
            return Body()
        form = self._private_form(draft, payload.form or {})
        if payload.kind == "form":
            text = urlencode(list(form.items()))
            return Body(kind="form", content_type=content_type, text=text, size=size)
        parts = [MultipartPart(name=k, text=v, size=len(v.encode())) for k, v in form.items()]
        for index, (planned, (name, (filename, content, file_type))) in enumerate(
            zip(request.files, payload.files or [], strict=True)
        ):
            part = MultipartPart(
                name=name, filename=filename, content_type=file_type, size=len(content)
            )
            if planned.text is not None:
                part.text = self.privacy.text(planned.text)
            else:
                sidecar = self.store.sidecar_name(draft.exchange_id, f"part{index}")
                draft.sidecars[sidecar] = content
                part.file = sidecar
            parts.append(part)
        return Body(kind="multipart", content_type=content_type, parts=parts, size=size)

    def _private_form(self, draft: _Draft, form: Mapping[str, str]) -> dict[str, str]:
        """Form fields through the JSON privacy rules (``password`` redacted, emails replaced)."""
        if not form:
            return {}
        fields: dict[str, JsonValue] = dict(form)
        value, red, pseudo = self.privacy.json(fields)
        draft.request_notes += [f"request.form:{p}" for p in red]
        draft.pseudonymized += [f"request.form:{p}" for p in pseudo]
        items = value.items() if isinstance(value, dict) else fields.items()
        return {name: _state_text(item) for name, item in items}

    # -- streams ---------------------------------------------------------------------------------

    async def _stream(
        self, call: PlannedCall, spec: StreamSpec, variables: Mapping[str, JsonValue]
    ) -> tuple[Exchange, JsonValue]:
        origin = _Origin(call.endpoint, call.purpose, call.id, list(call.tags), call.request)
        built = self._build(call.request, variables)
        payload = self._payload(call.request, built, from_mapping(dict(variables)))
        draft = _Draft(self.store.next_id())
        started_ns = self._clock_ns()
        started = time.perf_counter()
        if spec.kind == "websocket":
            built.url = _ws_url(built.url)
            received = await self._record_websocket(built, spec, started, draft)
        else:
            received = await self._record_http_stream(call.request, built, payload, spec, started)
        received.started_ns = started_ns
        received.elapsed_ms = (time.perf_counter() - started) * 1000
        raw = received.body.parsed() if received.body is not None else None
        exchange = self._exchange(draft, origin, built, payload, received)
        self.store.save(exchange, draft.sidecars)
        return exchange, raw

    async def _record_http_stream(
        self,
        request: PlannedRequest,
        built: _Built,
        payload: _Payload,
        spec: StreamSpec,
        started: float,
    ) -> _Received:
        """SSE / chunked: the planned method and body; an error response is kept as a body."""
        timeout = httpx2.Timeout(
            self.target.timeout_s, read=spec.duration_s + self.target.timeout_s
        )
        async with self.http.stream(
            request.method.value,
            built.url,
            headers=built.headers,
            content=payload.content,
            files=payload.files,
            data=payload.form,
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            received = _Received.of(response, started)
            if 200 <= response.status_code < 300:  # noqa: PLR2004 - success range
                received.events = await _read_events(response, spec, started)
            else:
                received.classify(await response.aread())
        return received

    async def _record_websocket(
        self, built: _Built, spec: StreamSpec, started: float, draft: _Draft
    ) -> _Received:
        """A WebSocket conversation; a refused upgrade is recorded with its status and body."""
        from httpx2.websockets import HTTPXWSException, WebSocketUpgradeError  # noqa: PLC0415

        received = _Received(status=101)
        try:
            async with self.http.websocket(
                built.url, headers=built.headers, timeout=self.target.timeout_s
            ) as session:
                handshake = session.response
                if handshake is not None:
                    received = _Received.of(handshake, started)
                await self._converse(session, spec, started, received, draft)
        except WebSocketUpgradeError as refused:
            received = _Received.of(refused.response, started)
            raw = b""
            with contextlib.suppress(httpx2.HTTPError, RuntimeError):  # body may be unreadable
                raw = await refused.response.aread()
            received.classify(raw)
        except HTTPXWSException as exc:
            received.note = f"websocket error: {type(exc).__name__}: {exc}"[:300]
        return received

    async def _converse(
        self,
        session: AsyncWebSocketSession,
        spec: StreamSpec,
        started: float,
        received: _Received,
        draft: _Draft,
    ) -> None:
        from httpx2.websockets import HTTPXWSException  # noqa: PLC0415

        events = received.events
        deadline = started + spec.duration_s

        def stamp() -> float:
            return round((time.perf_counter() - started) * 1000, 3)

        try:
            for message in spec.send:
                text = message if isinstance(message, str) else jsonfmt.dumps(message)
                events.append(StreamEvent(t_ms=stamp(), kind="ws_send", data=text))
                await session.send_text(text)
            async with self._frames_sent(session, spec.frames, started, received, draft):
                await self._receive(session, spec, deadline, stamp, received, draft)
        except HTTPXWSException as exc:
            received.note = f"websocket error: {type(exc).__name__}: {exc}"[:300]

    @contextlib.asynccontextmanager
    async def _frames_sent(
        self,
        session: AsyncWebSocketSession,
        frames: Sequence[WsFrame],
        started: float,
        received: _Received,
        draft: _Draft,
    ) -> AsyncIterator[None]:
        """Send ``frames`` on their schedule while the body receives; unsent ones are noted."""
        from httpx2.websockets import HTTPXWSException  # noqa: PLC0415

        if not frames:
            yield
            return
        sent = 0

        async def send_all() -> None:
            nonlocal sent
            for frame in frames:
                wait = started + frame.at_ms / 1000 - time.perf_counter()
                if wait > 0:
                    await asyncio.sleep(wait)  # real time: the conversation runs on it
                t_ms = round((time.perf_counter() - started) * 1000, 3)
                payload = frame.payload()
                if isinstance(payload, bytes):
                    await session.send_bytes(payload)
                    name = self.store.sidecar_name(
                        draft.exchange_id, f"ws{len(received.events)}-sent"
                    )
                    draft.sidecars[name] = payload
                    event = StreamEvent(t_ms=t_ms, kind="ws_send", binary_file=name)
                else:
                    await session.send_text(payload)
                    event = StreamEvent(t_ms=t_ms, kind="ws_send", data=payload)
                received.events.append(event)
                sent += 1

        sender = asyncio.create_task(send_all())
        try:
            yield
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, HTTPXWSException):
                await sender
            received.events.sort(key=lambda event: event.t_ms)  # sends and receives interleave
            if sent < len(frames):
                unsent = f"{len(frames) - sent} of {len(frames)} frames not sent (connection ended)"
                received.note = f"{received.note}; {unsent}" if received.note else unsent

    async def _receive(
        self,
        session: AsyncWebSocketSession,
        spec: StreamSpec,
        deadline: float,
        stamp: Callable[[], float],
        received: _Received,
        draft: _Draft,
    ) -> None:
        from httpx2.websockets import (  # noqa: PLC0415
            WebSocketDisconnect,
            WebSocketInvalidTypeReceived,
        )

        events = received.events
        while len(events) < spec.max_events:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            try:
                text = await session.receive_text(timeout=remaining)
            except TimeoutError:
                break
            except WebSocketDisconnect as closed:
                reason = closed.reason or None
                events.append(
                    StreamEvent(t_ms=stamp(), kind="ws_close", code=closed.code, data=reason)
                )
                break
            except WebSocketInvalidTypeReceived as other:  # a binary message
                data = getattr(other.event, "data", b"")
                name = self.store.sidecar_name(draft.exchange_id, f"ws{len(events)}")
                draft.sidecars[name] = data if isinstance(data, bytes) else bytes(data)
                events.append(StreamEvent(t_ms=stamp(), kind="ws_recv", binary_file=name))
                continue
            events.append(StreamEvent(t_ms=stamp(), kind="ws_recv", data=text))


def host_of(url: str) -> str:
    """The host part of a URL (used in reports)."""
    return urlsplit(url).netloc
