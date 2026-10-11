"""Helpers used by every generated fake's test suite."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import httpx2

from fauxcall import jsonfmt
from fauxcall.errors import RecordingError
from fauxcall.model.api import ApiModel
from fauxcall.recording.store import RecordingStore
from fauxcall.storage import read_model
from fauxcall.testing.client import placeholder_credentials, with_query
from fauxcall.testing.compare import shape_differences
from fauxcall.testing.plugin import FakeUnderTest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fastapi import FastAPI

    from fauxcall.config import FakeSettings
    from fauxcall.model.auth import AuthModel
    from fauxcall.model.recording import Exchange
    from fauxcall.testing.compare import Translate
    from fauxcall.types import JsonValue

REPLAY_PURPOSES: Final = frozenset(
    {"sample", "variation", "edge", "invalid", "pagination", "auth", "time_profile"}
)
STATEFUL_ROLES: Final = frozenset(
    {
        "create",
        "replace",
        "update",
        "delete",
        "job_submit",
        "job_status",
        "download",
        "upload",
        "sse",
        "websocket",
    }
)
"""Roles whose recordings depend on state created while recording (or on a stream)."""
REPLAYED_MCP_METHODS: Final = (
    "MCP tools/call:",
    "MCP tools/list",
    "MCP resources/",
    "MCP prompts/",
)
_DROPPED_HEADERS: Final = frozenset({"host", "content-length", "accept-encoding", "connection"})
_MAX_SHAPE_PROBLEMS: Final = 5
_UNAUTHORIZED: Final = 401


def fake_under_test[S: FakeSettings](
    settings_cls: type[S], build_app: Callable[[S], FastAPI]
) -> FakeUnderTest:
    """Describe a fake for the parity plugin from its settings class and app factory.

    The fake runs in fast mode with quiet logs; the real side's base URL and credentials come from
    the settings (``.env``), the pseudonym key turns real personal values into the fake's.
    """
    settings = settings_cls()
    model = read_model(settings.data_dir / "model.json", ApiModel)
    translate: Translate | None = None
    if settings.pseudonym_key is not None:
        from fauxcall.privacy.pseudonym import Pseudonymizer  # noqa: PLC0415

        translate = Pseudonymizer(settings.pseudonym_key.get_secret_value()).translate

    def factory() -> FastAPI:
        return build_app(settings_cls(fast=True, log_level="WARNING"))

    return FakeUnderTest(
        app_factory=factory,
        model=model,
        real_base_url=settings.real.base_url or model.real.base_url,
        real_headers={k: v.get_secret_value() for k, v in settings.real.headers.items()},
        real_query={k: v.get_secret_value() for k, v in settings.real.query.items()},
        reports_dir=settings.data_dir / "reports",
        translate=translate,
        mcp_path=model.mcp.transport.path if model.mcp else "/mcp",
    )


@dataclass(frozen=True, slots=True)
class RecordedCase:
    """One recorded request to replay against the fake."""

    id: str
    exchange: Exchange
    mcp: bool
    auth: AuthModel | None = None
    """The API's auth schemes: recorded (redacted) credentials are replaced by placeholders."""


@dataclass(slots=True)
class ReplayOutcome:
    """How the fake's answer compares with the recording."""

    case: RecordedCase
    expected_status: int | None
    actual_status: int | None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """No problems found."""
        return not self.problems

    def explain(self) -> str:
        """Readable failure message."""
        exchange = self.case.exchange
        request = exchange.request
        header = f"{exchange.endpoint} ({exchange.id}, {request.method.value} {request.path})"
        return header + ":\n  - " + "\n  - ".join(self.problems)


def recorded_cases(
    data_dir: Path,
    *,
    purposes: frozenset[str] = REPLAY_PURPOSES,
    exclude_endpoints: frozenset[str] = frozenset(),
) -> list[RecordedCase]:
    """Recorded requests worth replaying (stateless endpoints, read-like purposes)."""
    model_path = data_dir / "model.json"
    if not model_path.exists():
        return []
    model = read_model(model_path, ApiModel)
    roles = {endpoint.id: endpoint.role for endpoint in model.endpoints}
    cases: list[RecordedCase] = []
    for _, exchange in RecordingStore(data_dir / "recordings").iter_exchanges():
        if exchange.response is None or exchange.purpose not in purposes:
            continue
        if exchange.endpoint in exclude_endpoints or roles.get(exchange.endpoint) in STATEFUL_ROLES:
            continue
        is_mcp = exchange.endpoint.startswith("MCP ")
        if is_mcp and not exchange.endpoint.startswith(REPLAYED_MCP_METHODS):
            continue
        if not is_mcp and exchange.endpoint not in roles:
            continue
        if exchange.request.body.kind in {"multipart", "binary"}:
            continue
        cases.append(
            RecordedCase(
                id=f"{exchange.id}-{exchange.endpoint}",
                exchange=exchange,
                mcp=is_mcp,
                auth=model.auth,
            )
        )
    return cases


def _auth_probe(exchange: Exchange) -> bool:
    """Recorded to capture the real auth errors: replayed without credentials."""
    response = exchange.response
    return exchange.purpose == "auth" or (response is not None and response.status == _UNAUTHORIZED)


@dataclass(frozen=True, slots=True)
class _ReplayCredentials:
    """Placeholder credentials for a replay, and where credentials go (to drop recorded ones)."""

    headers: dict[str, str]
    query: dict[str, str]
    header_names: frozenset[str]
    query_names: frozenset[str]


def _credentials(case: RecordedCase) -> _ReplayCredentials:
    """Placeholders for the API's schemes; none at all for auth probes."""
    credentials = placeholder_credentials(case.auth)
    headers = dict(credentials.headers)
    if credentials.cookies:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in credentials.cookies.items())
    header_names = frozenset(name.lower() for name in headers)
    query_names = frozenset(credentials.query)
    if _auth_probe(case.exchange):
        return _ReplayCredentials({}, {}, header_names, query_names)
    return _ReplayCredentials(headers, dict(credentials.query), header_names, query_names)


def _replay_parts(case: RecordedCase) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Headers and query of the recorded request, with placeholder credentials.

    Recordings hold redacted credentials (``Bearer [REDACTED]``) that the fake may reject, so
    every credential the API's schemes define is replaced by a well-formed placeholder, or left
    out for auth probes (so the fake answers with its "missing credentials" error).
    """
    request = case.exchange.request
    credentials = _credentials(case)
    dropped = _DROPPED_HEADERS | credentials.header_names
    headers = [(k, v) for k, v in request.headers if k.lower() not in dropped]
    query = [(k, v) for k, v in request.query if k not in credentials.query_names]
    return headers + list(credentials.headers.items()), query + list(credentials.query.items())


def _content(exchange: Exchange) -> bytes | None:
    body = exchange.request.body
    if body.kind == "json":
        return jsonfmt.dumps(body.json_value).encode()
    if body.kind in {"text", "form"} and body.text is not None:
        return body.text.encode()
    return None


async def replay_against(app: FastAPI, case: RecordedCase) -> ReplayOutcome:
    """Send a recorded request to the fake and compare status and body shape."""
    exchange = case.exchange
    expected = exchange.response
    assert expected is not None  # noqa: S101 - filtered by recorded_cases
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client:
        if case.mcp:
            return await _replay_mcp(client, case)
        headers, query = _replay_parts(case)
        response = await client.request(
            exchange.request.method.value,
            exchange.request.path,
            params=tuple(query),
            headers=headers,
            content=_content(exchange),
        )
    outcome = ReplayOutcome(case, expected.status, response.status_code)
    if response.status_code != expected.status:
        outcome.problems.append(f"status {response.status_code}, recorded {expected.status}")
    recorded_body = expected.body.parsed()
    if recorded_body is not None:
        try:
            actual: JsonValue = jsonfmt.loads(response.content) if response.content else None
        except ValueError:
            outcome.problems.append("recorded a JSON body, the fake answered non-JSON")
            return outcome
        differences = shape_differences(recorded_body, actual)
        outcome.problems += [f"shape: {d}" for d in differences[:_MAX_SHAPE_PROBLEMS]]
    return outcome


async def _replay_mcp(client: httpx2.AsyncClient, case: RecordedCase) -> ReplayOutcome:
    from fauxcall.mcpwire.client import McpWireClient  # noqa: PLC0415

    exchange = case.exchange
    expected = exchange.response
    assert expected is not None  # noqa: S101 - filtered by recorded_cases
    sent = exchange.request.body.parsed()
    recorded = expected.body.parsed()
    method = str(sent.get("method")) if isinstance(sent, dict) else ""
    params = sent.get("params") if isinstance(sent, dict) else None
    credentials = _credentials(case)  # recorded MCP headers carry stale sessions: not replayed
    url = with_query(str(client.base_url).rstrip("/") + exchange.request.path, credentials.query)
    wire = McpWireClient(url, client, headers=credentials.headers)
    try:
        await wire.initialize()
        call = await wire.call(method, params)
    except RecordingError as exc:
        return ReplayOutcome(case, expected.status, None, [str(exc)])
    finally:
        await wire.close()
    outcome = ReplayOutcome(case, expected.status, call.status)
    if call.status != expected.status:
        outcome.problems.append(f"status {call.status}, recorded {expected.status}")
    actual = call.response if isinstance(call.response, dict) else {}
    if not isinstance(recorded, dict):
        return outcome
    for member in ("result", "error"):
        if (member in recorded) != (member in actual):
            outcome.problems.append(
                f"recorded has {member!r}: {member in recorded}, fake: {member in actual}"
            )
    if "result" in recorded and "result" in actual:
        differences = shape_differences(recorded["result"], actual["result"])
        outcome.problems += [f"shape: {d}" for d in differences[:_MAX_SHAPE_PROBLEMS]]
    return outcome
