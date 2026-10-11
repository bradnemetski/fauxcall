"""Target-agnostic clients for end-to-end scenarios (real API, fake, or both for parity)."""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest

from fauxcall import jsonfmt
from fauxcall.testing.compare import Observation

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from fauxcall.mcpwire.client import McpWireClient
    from fauxcall.model.auth import AuthModel, AuthScheme
    from fauxcall.model.fields import FieldPolicy
    from fauxcall.privacy.pii import PiiCategory
    from fauxcall.testing.compare import Translate
    from fauxcall.types import JsonValue

Target = Literal["real", "fake"]
_SAFE: Final = frozenset({"GET", "HEAD", "OPTIONS"})
MCP_METHOD: Final = "MCP"
"""``LoggedCall.method`` of MCP calls; their ``path`` is the endpoint detail (``tools/call:x``)."""
PLACEHOLDER_KEY: Final = "fauxcall-test-key"
PLACEHOLDER_TOKEN: Final = "fauxcall-test-token"  # noqa: S105 - made up, not a secret
PLACEHOLDER_USER: Final = "fauxcall"
PLACEHOLDER_PASSWORD: Final = "fauxcall"  # noqa: S105 - made up, not a secret
_FALLBACK_SECRETS: Final = (
    "fauxcalltestkey",
    "0123456789abcdef0123456789abcdef",
    "00000000-0000-4000-8000-000000000000",
    "0123456789abcdef0123456789abcdef01234567",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "1234567890",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYXV4Y2FsbCJ9.ZmF1eGNhbGw",
)
"""Tried in order when a scheme's ``format_regex`` rejects the first choice."""


@dataclass(frozen=True, slots=True)
class Credentials:
    """Credentials sent with every request; a request's own headers and query parameters win."""

    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


def _secret(scheme: AuthScheme, preferred: str) -> str:
    """A made-up secret that satisfies the scheme's ``format_regex`` when one of ours can."""
    if not scheme.format_regex:
        return preferred
    try:
        pattern = re.compile(scheme.format_regex)
    except re.error:
        return preferred
    return next((c for c in (preferred, *_FALLBACK_SECRETS) if pattern.fullmatch(c)), preferred)


def placeholder_credentials(auth: AuthModel | None) -> Credentials:
    """Well-formed, made-up credentials for every scheme of the API (never the real ones).

    The fake accepts any well-formed credential by default (auth mode ``any``), so scenarios
    against it pass without secrets: API keys go in their header, query parameter or cookie;
    the first ``Authorization``-based scheme (bearer/OAuth token, Basic, or an API key named
    ``Authorization``) gets that header.
    """
    credentials = Credentials()
    if auth is None:
        return credentials
    authorization = False
    for scheme in auth.schemes:
        if scheme.kind == "api_key":
            name = scheme.name or ""
            value = (scheme.prefix or "") + _secret(scheme, PLACEHOLDER_KEY)
            is_authorization = scheme.location not in {"query", "cookie"} and (
                name.lower() == "authorization"
            )
            if scheme.location == "query":
                credentials.query.setdefault(name, value)
            elif scheme.location == "cookie":
                credentials.cookies.setdefault(name, value)
            elif not (is_authorization and authorization):
                credentials.headers.setdefault(name, value)
            authorization = authorization or is_authorization
        elif scheme.kind in {"bearer", "oauth2"} and not authorization:
            token = _secret(scheme, PLACEHOLDER_TOKEN)
            credentials.headers["Authorization"] = (scheme.prefix or "Bearer ") + token
            authorization = True
        elif scheme.kind == "basic" and not authorization:
            password = _secret(scheme, PLACEHOLDER_PASSWORD)
            encoded = base64.b64encode(f"{PLACEHOLDER_USER}:{password}".encode()).decode()
            credentials.headers["Authorization"] = f"Basic {encoded}"
            authorization = True
    return credentials


def with_query(path: str, query: Mapping[str, str]) -> str:
    """``path`` plus the parameters of ``query`` it does not set itself."""
    present = parse_qs(urlsplit(path).query, keep_blank_values=True)
    missing = {key: value for key, value in query.items() if key not in present}
    if not missing:
        return path
    return path + ("&" if "?" in path else "?") + urlencode(missing)


@dataclass(slots=True)
class CompareOptions:
    """Per-call tweaks for parity comparison."""

    values: bool = True
    lengths: bool = True
    policies: list[FieldPolicy] = field(default_factory=list)


@dataclass(slots=True)
class LoggedCall:
    """One call made by a scenario."""

    label: str
    method: str
    path: str
    observation: Observation
    options: CompareOptions


@dataclass(slots=True)
class ExchangeLog:
    """Ordered calls of one scenario run."""

    calls: list[LoggedCall] = field(default_factory=list)


def _observe(response: httpx2.Response, at_ns: int) -> Observation:
    headers = {key.lower(): value for key, value in response.headers.items()}
    body: JsonValue = None
    text: str | None = None
    content_type = headers.get("content-type", "")
    if response.content:
        if "json" in content_type:
            try:
                body = jsonfmt.loads(response.content)
            except ValueError:
                text = response.text
        elif "event-stream" in content_type or content_type.startswith("text/"):
            text = response.text
        else:
            text = f"<{len(response.content)} bytes>"
    return Observation(
        status=response.status_code, headers=headers, body=body, text=text, at_ns=at_ns
    )


def _midpoint(started_ns: int) -> int:
    """When the server most likely produced its answer: halfway through the round trip."""
    return (started_ns + time.time_ns()) // 2


class ApiClient:
    """HTTP client that logs every call for parity comparison."""

    def __init__(
        self,
        target: Target,
        http: httpx2.AsyncClient,
        *,
        allow_writes: bool,
        translate: Translate | None = None,
        query: Mapping[str, str] | None = None,
        log: ExchangeLog | None = None,
    ) -> None:
        self.target: Target = target
        self.http = http
        self.allow_writes = allow_writes
        self.translate = translate
        self.query = dict(query or {})
        """Parameters (credentials) added to every request that does not set them itself."""
        self.log = log if log is not None else ExchangeLog()

    @property
    def is_real(self) -> bool:
        """Whether this run talks to the real API."""
        return self.target == "real"

    def pii(self, value: JsonValue, category: PiiCategory) -> JsonValue:
        """A personal value as each side knows it (pseudonym on the fake)."""
        if self.target == "fake" and self.translate is not None:
            return self.translate(value, category)
        return value

    async def request(
        self,
        method: str,
        path: str,
        *,
        label: str | None = None,
        compare: CompareOptions | None = None,
        **kwargs: object,
    ) -> httpx2.Response:
        """Send a request and log what came back (``kwargs`` go to httpx2).

        Writes (anything but GET/HEAD/OPTIONS) to the real API skip the test unless the run
        allows them (``--allow-real-writes``).
        """
        upper = method.upper()
        if self.is_real and upper not in _SAFE and not self.allow_writes:
            pytest.skip(
                "writes to the real API need --allow-real-writes (and an up-front approval)"
            )
        options = compare or CompareOptions()
        url = self._with_credentials(path, kwargs)
        started_ns = time.time_ns()
        try:
            response = await self.http.request(upper, url, **kwargs)  # type: ignore[arg-type]
        except httpx2.HTTPError as exc:
            observation = Observation(None, {}, None, None, started_ns, error=type(exc).__name__)
            self.log.calls.append(
                LoggedCall(label or f"{upper} {path}", upper, path, observation, options)
            )
            raise
        observation = _observe(response, _midpoint(started_ns))
        self.log.calls.append(
            LoggedCall(label or f"{upper} {path}", upper, path, observation, options)
        )
        return response

    def _with_credentials(self, path: str, kwargs: dict[str, object]) -> str:
        """Add the query credentials (httpx2 replaces a URL's query when ``params`` are given)."""
        if not self.query:
            return path
        if kwargs.get("params") is None:
            return with_query(path, self.query)
        params = httpx2.QueryParams(cast("httpx2.QueryParams", kwargs["params"]))
        for key, value in self.query.items():
            if key not in params:
                params = params.add(key, value)
        kwargs["params"] = params
        return path

    async def get(self, path: str, **kwargs: object) -> httpx2.Response:
        """GET."""
        return await self.request("GET", path, **kwargs)  # type: ignore[arg-type]

    async def post(self, path: str, **kwargs: object) -> httpx2.Response:
        """POST."""
        return await self.request("POST", path, **kwargs)  # type: ignore[arg-type]

    async def put(self, path: str, **kwargs: object) -> httpx2.Response:
        """PUT."""
        return await self.request("PUT", path, **kwargs)  # type: ignore[arg-type]

    async def patch(self, path: str, **kwargs: object) -> httpx2.Response:
        """PATCH."""
        return await self.request("PATCH", path, **kwargs)  # type: ignore[arg-type]

    async def delete(self, path: str, **kwargs: object) -> httpx2.Response:
        """DELETE."""
        return await self.request("DELETE", path, **kwargs)  # type: ignore[arg-type]


def mcp_call_path(method: str, params: JsonValue) -> str:
    """The endpoint detail of an MCP call (``tools/call:<tool>``, ``resources/read:<uri>``).

    Matches FauxCall's MCP endpoint ids (``MCP tools/call:<tool>``) once prefixed with ``MCP``.
    """
    detail: JsonValue = None
    if isinstance(params, dict):
        if method in {"tools/call", "prompts/get"}:
            detail = params.get("name")
        elif method == "resources/read":
            detail = params.get("uri")
    return f"{method}:{detail}" if isinstance(detail, str) and detail else method


class McpClient:
    """MCP scenario client: same idea, JSON-RPC results compared instead of HTTP bodies."""

    def __init__(
        self,
        target: Target,
        wire: McpWireClient,
        *,
        allow_writes: bool,
        mutating: Callable[[str], bool],
        log: ExchangeLog | None = None,
    ) -> None:
        self.target: Target = target
        self.wire = wire
        self.allow_writes = allow_writes
        self.mutating = mutating
        self.log = log if log is not None else ExchangeLog()
        self._open = False

    async def _ensure(self) -> None:
        if not self._open:
            await self.wire.initialize()
            self._open = True

    async def request(
        self,
        method: str,
        params: JsonValue = None,
        *,
        label: str | None = None,
        compare: CompareOptions | None = None,
    ) -> JsonValue:
        """Call a JSON-RPC method and return its ``result`` (or ``error``).

        The logged observation has status 200 for a result and 500 for an error (or no answer).
        """
        await self._ensure()
        path = mcp_call_path(method, params)
        options = compare or CompareOptions()
        started_ns = time.time_ns()
        try:
            call = await self.wire.call(method, params)
        except httpx2.HTTPError as exc:
            observation = Observation(None, {}, None, None, started_ns, error=type(exc).__name__)
            self.log.calls.append(LoggedCall(label or path, MCP_METHOD, path, observation, options))
            raise
        message = call.response if isinstance(call.response, dict) else {}
        payload = message.get("result", message.get("error"))
        observation = Observation(
            status=200 if "result" in message else 500,
            headers={"content-type": "application/json"},
            body=payload,
            text=None,
            at_ns=_midpoint(started_ns),
        )
        self.log.calls.append(LoggedCall(label or path, MCP_METHOD, path, observation, options))
        return payload

    async def list_tools(self) -> JsonValue:
        """``tools/list``."""
        return await self.request("tools/list", label="tools/list")

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, JsonValue] | None = None,
        *,
        label: str | None = None,
        compare: CompareOptions | None = None,
    ) -> JsonValue:
        """``tools/call``; tools that may modify state skip real runs without permission."""
        if self.target == "real" and self.mutating(name) and not self.allow_writes:
            pytest.skip(f"tool {name!r} may modify state: needs --allow-real-writes")
        params: JsonValue = {"name": name, "arguments": dict(arguments or {})}
        return await self.request(
            "tools/call", params, label=label or f"tools/call:{name}", compare=compare
        )

    async def close(self) -> None:
        """End the MCP session."""
        if self._open:
            self._open = False
            await self.wire.close()
