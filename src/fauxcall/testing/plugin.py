"""pytest plugin for a fake's end-to-end suite.

Enable it with ``-p fauxcall.testing.plugin`` (generated fakes do so in ``pyproject.toml``) or in a
``conftest.py``::

    pytest_plugins = ["fauxcall.testing.plugin"]

and provide a ``fauxcall_fake`` fixture returning a :class:`FakeUnderTest`. Scenarios use the
``api`` (REST) or ``mcp`` fixture and run against:

* ``--target=fake`` (default): the fake, in-process — fast, offline, CI-safe;
* ``--target=real``: the real API (credentials from the fake's ``.env``);
* ``--target=parity``: real first, then fake; every call is compared and a compliance report is
  written. The session fails below ``--parity-threshold`` (default 0.98), and when a scenario ran
  on one side only (e.g. no real API is configured).

Writes to the real API are refused unless ``--allow-real-writes`` is given.
"""

from __future__ import annotations

import contextlib
import dataclasses
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import httpx2
import pytest
import pytest_asyncio

from fauxcall.mcpwire.client import McpWireClient
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import StreamingASGITransport
from fauxcall.testing.client import (
    ApiClient,
    ExchangeLog,
    McpClient,
    RealPace,
    WriteBudget,
    placeholder_credentials,
    with_query,
)
from fauxcall.testing.compare import W_STATUS, Comparison, compare
from fauxcall.testing.replays import recorded_keys
from fauxcall.testing.report import ParityReport, endpoint_for, endpoint_matcher

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence

    from fastapi import FastAPI

    from fauxcall.model.api import ApiModel
    from fauxcall.model.endpoints import EndpointModel
    from fauxcall.privacy.pii import PiiCategory
    from fauxcall.recording.privacy_filter import RecordingPrivacy
    from fauxcall.testing.client import LoggedCall, Target
    from fauxcall.testing.compare import Observation, Translate
    from fauxcall.types import JsonValue

DEFAULT_THRESHOLD: Final = 0.98
FAKE_BASE_URL: Final = "http://fake.test"
REAL_BASE_URL: Final = "http://real.test"
"""Base URL of the real side when it is reached through ``FakeUnderTest.real_transport``."""
REPORTS_DIR: Final = Path("parity-reports")
"""Where reports go when the fake does not say (relative to the working directory)."""
_RECORD_ROLES: Final = frozenset({"get", "create", "replace", "update", "job_submit", "job_status"})
_LIST_ROLES: Final = frozenset({"list", "search"})


@dataclass(slots=True)
class FakeUnderTest:
    """How the plugin reaches the fake and the real API."""

    app_factory: Callable[[], FastAPI]
    model: ApiModel | None = None
    real_base_url: str | None = None
    real_headers: dict[str, str] = field(default_factory=dict)
    real_query: dict[str, str] = field(default_factory=dict)
    reports_dir: Path | None = None
    approvals_path: Path | None = None
    """``data/approvals.json``: what the real side's writes may spend (none: nothing is written)."""
    translate: Translate | None = None
    mcp_path: str = "/mcp"
    real_transport: httpx2.AsyncBaseTransport | None = None
    """Reach the real side through this transport (an in-process app …) rather than the network."""
    recordings_dir: Path | None = None
    """``data/recordings``: scenario calls that repeat one of these count apart in the report."""
    privacy: RecordingPrivacy | None = None
    """The fake's privacy filter: the real side's answers go through it before they are compared,
    so they look as they would have been recorded (same pseudonyms as the fake, credentials
    redacted) and no real personal data reaches the report."""


@dataclass(slots=True)
class ScenarioRuns:
    """The calls one scenario made on each side."""

    spec: FakeUnderTest
    real: ExchangeLog | None = None
    fake: ExchangeLog | None = None


@dataclass(slots=True)
class ParitySession:
    """What one pytest session observed (kept in ``config.stash``, so sessions never mix)."""

    runs: dict[str, ScenarioRuns] = field(default_factory=dict)
    report: ParityReport | None = None
    paths: tuple[Path, Path] | None = None
    recorded: dict[Path, frozenset[str]] = field(default_factory=dict)
    """Recorded request keys per recordings folder, read once per session."""

    def recorded_keys(self, spec: FakeUnderTest) -> frozenset[str]:
        """The keys of the fake's recorded requests (none when it names no recordings)."""
        if spec.recordings_dir is None:
            return frozenset()
        if spec.recordings_dir not in self.recorded:
            names = _credential_names(spec)
            self.recorded[spec.recordings_dir] = recorded_keys(spec.recordings_dir, names)
        return self.recorded[spec.recordings_dir]


_SESSION: Final = pytest.StashKey[ParitySession]()


def parity_session(config: pytest.Config) -> ParitySession:
    """This session's parity state (created on first use)."""
    state = config.stash.get(_SESSION, None)
    if state is None:
        state = config.stash[_SESSION] = ParitySession()
    return state


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register FauxCall's command-line options."""
    group = parser.getgroup("fauxcall")
    group.addoption(
        "--target", choices=["fake", "real", "parity"], default="fake", help="Where scenarios run."
    )
    group.addoption(
        "--parity-threshold", type=float, default=DEFAULT_THRESHOLD, help="Minimum compliance."
    )
    group.addoption(
        "--fake-url", default=None, help="Test a running fake instead of the in-process app."
    )
    group.addoption(
        "--allow-real-writes", action="store_true", help="Permit writes to the real API."
    )


def _target(config: pytest.Config) -> str:
    value: str = config.getoption("--target")
    return value


@pytest.fixture(autouse=True)
def _fauxcall_offline(request: pytest.FixtureRequest) -> Iterator[None]:
    """Against the fake alone, nothing may reach the network: a fake answers from what it holds.

    A fake that fetched URLs or called the real service would pass while the real service
    happened to be up and fail anywhere else (CI, another machine). Connections to the local
    machine stay open (in-process servers, test sockets), except to the real service's own
    address; any other attempt fails the test, even if the fake swallowed the error.
    """
    config = request.config
    if _target(config) != "fake" or config.getoption("--fake-url"):
        yield
        return
    real = _real_address(request)
    attempts: list[str] = []
    original = socket.socket.connect

    def guarded(sock: socket.socket, address: Any) -> None:  # noqa: ANN401 - socket addresses
        if _offline_refuses(sock, address, real):
            attempts.append(f"{address[0]}:{address[1]}")
            raise ConnectionRefusedError(f"fauxcall: a fake can't reach the network ({address})")
        original(sock, address)

    setattr(socket.socket, "connect", guarded)  # noqa: B010 - replacing a method for the run
    try:
        yield
    finally:
        setattr(socket.socket, "connect", original)  # noqa: B010
    if attempts:
        pytest.fail(
            "the fake tried to reach the network ("
            + ", ".join(dict.fromkeys(attempts))
            + "): a fake must answer from its recordings and data, never by calling out",
            pytrace=False,
        )


def _real_address(request: pytest.FixtureRequest) -> tuple[str, int] | None:
    """The real service's (host, port), when the fake under test names it."""
    try:
        spec = request.getfixturevalue("fauxcall_fake")
    except pytest.FixtureLookupError:
        return None
    base = spec.real_base_url or (spec.model.real.base_url if spec.model else None)
    if not base:
        return None
    parts = urlsplit(base)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return (parts.hostname or "", port)


def _offline_refuses(sock: socket.socket, address: Any, real: tuple[str, int] | None) -> bool:  # noqa: ANN401
    """Whether an offline run refuses this connection: off the machine, or to the real service."""
    if sock.family not in {socket.AF_INET, socket.AF_INET6} or not isinstance(address, tuple):
        return False
    host, port = str(address[0]), int(address[1])
    try:
        local = ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        local = host == "localhost"
    if not local:
        return True
    if real is None:
        return False
    real_host, real_port = real
    real_local = real_host in {"localhost", ""} or _loopback(real_host)
    return real_local and port == real_port


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _as_target(value: str) -> Target:
    return "real" if value == "real" else "fake"


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """In parity mode every scenario runs twice: real, then fake."""
    if _target(metafunc.config) != "parity":
        return
    if "api" in metafunc.fixturenames or "mcp" in metafunc.fixturenames:
        metafunc.parametrize(
            "fauxcall_target", ["real", "fake"], ids=["real", "fake"], scope="function"
        )


@pytest.fixture
def fauxcall_target(request: pytest.FixtureRequest) -> str:
    """Which side this scenario run talks to."""
    param = getattr(request, "param", None)
    return str(param) if param else _target(request.config).replace("parity", "fake")


def scenario_id(node: pytest.Item | pytest.FixtureRequest) -> str:
    """The test id without the real/fake parameter, shared by both runs of a scenario."""
    nodeid = node.nodeid if isinstance(node, pytest.Item) else node.node.nodeid
    return re.sub(r"\[(real|fake)\]$|(?<=\[)(real|fake)-|-(real|fake)(?=\])", "", nodeid)


def _real_client(fake: FakeUnderTest) -> httpx2.AsyncClient:
    options: dict[str, Any] = {"headers": fake.real_headers, "timeout": 60.0}
    if fake.real_transport is not None:
        return httpx2.AsyncClient(
            transport=fake.real_transport, base_url=fake.real_base_url or REAL_BASE_URL, **options
        )
    if not fake.real_base_url:
        pytest.skip("no real API configured (set FAUXCALL_REAL__BASE_URL in the fake's .env)")
    return httpx2.AsyncClient(base_url=fake.real_base_url, **options)


def _fake_client(
    fake: FakeUnderTest, config: pytest.Config
) -> tuple[httpx2.AsyncClient, dict[str, str]]:
    """The fake, with made-up credentials for the API's auth schemes (never the real ones)."""
    credentials = placeholder_credentials(fake.model.auth if fake.model else None)
    options: dict[str, Any] = {
        "headers": credentials.headers,
        "cookies": credentials.cookies,
        "timeout": 60.0,
    }
    url = config.getoption("--fake-url")
    if url:
        return httpx2.AsyncClient(base_url=url, **options), credentials.query
    # streamed like a real server's answers: a client can read the server's requests mid-call
    # (sampling, elicitation, roots) and answer them, and endless streams don't hang the test
    transport = StreamingASGITransport(_fake_app(config, fake))
    client = httpx2.AsyncClient(transport=transport, base_url=FAKE_BASE_URL, **options)
    return client, credentials.query


_APPS_KEY: Final = pytest.StashKey[dict[int, "FastAPI"]]()


def _fake_app(config: pytest.Config, fake: FakeUnderTest) -> FastAPI:
    """The fake's app, built once per session and reset to the recorded state for each scenario.

    A reset is as good as a fresh app (world overlays, clocks, journal, counters, jobs, uploads)
    and much faster than loading a large world again.
    """
    apps = config.stash.setdefault(_APPS_KEY, {})
    app = apps.get(id(fake))
    if app is None:
        app = apps[id(fake)] = fake.app_factory()
        return app
    with contextlib.suppress(AttributeError):  # an app that isn't a FauxCall fake
        runtime_of_app(app).reset(journal=True)
    return app


def _side(
    fake: FakeUnderTest, target: str, config: pytest.Config
) -> tuple[httpx2.AsyncClient, dict[str, str]]:
    """The HTTP client for one side and the query parameters (credentials) every call carries.

    Query credentials are added per request, never as client ``params``: httpx2 would replace the
    query string scenarios write into their paths (``/pets?status=sold``).
    """
    if target == "real":
        return _real_client(fake), dict(fake.real_query)
    return _fake_client(fake, config)


def _keep(
    request: pytest.FixtureRequest, target: Target, log: ExchangeLog, fake: FakeUnderTest
) -> None:
    """Remember a finished run so the other side's run can be compared with it."""
    runs = parity_session(request.config).runs.setdefault(
        scenario_id(request), ScenarioRuns(spec=fake)
    )
    if target == "real":
        runs.real = log
    else:
        runs.fake = log


@pytest.fixture
def fauxcall_log() -> ExchangeLog:
    """The calls of the current scenario run, in order (shared by ``api`` and ``mcp``)."""
    return ExchangeLog()


@pytest_asyncio.fixture
async def api(
    request: pytest.FixtureRequest,
    fauxcall_fake: FakeUnderTest,
    fauxcall_target: str,
    fauxcall_log: ExchangeLog,
) -> AsyncIterator[ApiClient]:
    """The REST scenario client for the current target."""
    target = _as_target(fauxcall_target)
    http, query = _side(fauxcall_fake, target, request.config)
    matchers = endpoint_matcher(fauxcall_fake.model)
    client = ApiClient(
        target,
        http,
        allow_writes=bool(request.config.getoption("--allow-real-writes")),
        translate=fauxcall_fake.translate,
        query=query,
        log=fauxcall_log,
        pace=real_pace(request.config, fauxcall_fake) if target == "real" else None,
        budget=WriteBudget(
            fauxcall_fake.approvals_path,
            lambda method, path: endpoint_for(f"{method} {path}", matchers),
        )
        if target == "real"
        else None,
    )
    async with http:
        yield client
    _keep(request, target, fauxcall_log, fauxcall_fake)


@pytest_asyncio.fixture
async def mcp(
    request: pytest.FixtureRequest,
    fauxcall_fake: FakeUnderTest,
    fauxcall_target: str,
    fauxcall_log: ExchangeLog,
) -> AsyncIterator[McpClient]:
    """The MCP scenario client for the current target."""
    from fauxcall.recording.mcp import tool_is_mutating  # noqa: PLC0415

    target = _as_target(fauxcall_target)
    http, query = _side(fauxcall_fake, target, request.config)
    url = with_query(str(http.base_url).rstrip("/") + fauxcall_fake.mcp_path, query)
    model = fauxcall_fake.model

    def varies(name: str) -> bool:
        tool = model.mcp.tool(name) if model is not None and model.mcp is not None else None
        return tool is not None and tool.varies

    def mutating(name: str) -> bool:
        listed = model.mcp.tools if model is not None and model.mcp is not None else []
        tool = next((t for t in listed if t.name == name), None)
        if listed and tool is None:  # not a tool the server lists: it refuses it unknown
            return False
        return tool_is_mutating(tool.definition if tool else None)

    async with http:
        client = McpClient(
            target,
            McpWireClient(url, http),
            allow_writes=bool(request.config.getoption("--allow-real-writes")),
            mutating=mutating,
            varies=varies,
            log=fauxcall_log,
            pace=real_pace(request.config, fauxcall_fake) if target == "real" else None,
            budget=WriteBudget(fauxcall_fake.approvals_path, lambda _method, call: f"MCP {call}")
            if target == "real"
            else None,
        )
        try:
            yield client
        finally:
            await client.close()
    _keep(request, target, fauxcall_log, fauxcall_fake)


_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_PACE_KEY: Final = pytest.StashKey[RealPace]()


def real_pace(config: pytest.Config, fake: FakeUnderTest) -> RealPace:
    """The session's one pace for the real side (rate limits count every call of the run)."""
    if _PACE_KEY not in config.stash:
        config.stash[_PACE_KEY] = RealPace.for_model(fake.model)
    return config.stash[_PACE_KEY]


def record_roots(endpoint: EndpointModel) -> list[str] | None:
    """Where an endpoint's records sit in its responses (``None``: let the comparison guess)."""
    if endpoint.role in _LIST_ROLES:
        if endpoint.shape.kind == "envelope":
            return [f"{endpoint.shape.items_path or '$.data'}[*]"]
        return ["$[*]"]
    if endpoint.role in _RECORD_ROLES:
        return ["$"]
    return None


def settled(calls: Sequence[LoggedCall]) -> list[LoggedCall]:
    """The calls to compare: repeated identical reads in a row count once, with their last answer.

    That is a scenario polling a job until it finishes: how often each side polled depends on
    timing, not on fidelity.
    """
    kept: list[LoggedCall] = []
    for call in calls:
        polling = call.method.upper() in _SAFE_METHODS and bool(call.request)
        if kept and polling and kept[-1].request == call.request:
            kept[-1] = call
        else:
            kept.append(call)
    return kept


def compare_logs(
    scenario: str,
    real: ExchangeLog,
    fake: ExchangeLog,
    fake_spec: FakeUnderTest,
    recorded: frozenset[str] = frozenset(),
) -> list[Comparison]:
    """Compare two runs of the same scenario call by call.

    ``recorded``: the keys of the recorded requests; calls repeating one are marked ``replayed``.
    """
    model = fake_spec.model
    matchers = endpoint_matcher(model)
    comparisons: list[Comparison] = []
    aliases: dict[str, str] = {}  # fake id -> real id, learned from the scenario's writes
    real_calls, fake_calls = settled(real.calls), settled(fake.calls)
    for index, real_call in enumerate(real_calls):
        label = f"{real_call.method} {real_call.path}"
        if index >= len(fake_calls):
            missing = Comparison(label)
            missing.add(
                "call made",
                W_STATUS,
                passed=False,
                detail=f"{scenario}: the fake run stopped before this call",
            )
            comparisons.append(missing)
            continue
        endpoint = model.find_endpoint(endpoint_for(label, matchers)) if model else None
        options = real_call.options
        privacy = fake_spec.privacy
        comparison = compare(
            label,
            _as_recorded(real_call.observation, privacy),
            fake_calls[index].observation,
            policies=[*(endpoint.fields if endpoint else []), *options.policies],
            # Already pseudonymized as recorded: a pseudonym field compares as it is.
            translate=_unchanged if privacy is not None else fake_spec.translate,
            compare_lengths=options.lengths,
            max_value_checks=200 if options.values else 0,
            record_roots=record_roots(endpoint) if endpoint else None,
            aliases=aliases,
            created=real_call.method.upper() not in _SAFE_METHODS,
        )
        comparison.replayed = bool(real_call.replay_key) and real_call.replay_key in recorded
        comparison.shape_only = not options.values
        comparison.real_status = real_call.observation.status
        comparisons.append(comparison)
    return comparisons


def _unchanged(value: JsonValue, category: PiiCategory) -> JsonValue:
    del category
    return value


def _as_recorded(observation: Observation, privacy: RecordingPrivacy | None) -> Observation:
    """A real answer as the recorder would have stored it (personal data and secrets replaced)."""
    if privacy is None:
        return observation
    body = privacy.json(observation.body)[0] if observation.body is not None else None
    text = privacy.text(observation.text) if observation.text is not None else None
    return dataclasses.replace(observation, body=body, text=text)


def _credential_names(spec: FakeUnderTest) -> tuple[str, ...]:
    """Query parameters that carry credentials: left out of request keys."""
    schemes = spec.model.auth.schemes if spec.model and spec.model.auth else []
    names = [s.name for s in schemes if s.kind == "api_key" and s.location == "query" and s.name]
    return tuple(sorted({*names, *spec.real_query}))


def _report(state: ParitySession, config: pytest.Config) -> ParityReport:
    if state.report is None:
        state.report = ParityReport(threshold=config.getoption("--parity-threshold"))
    return state.report


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Once both runs of a scenario finished (fixtures torn down), compare them."""
    config = item.config
    if _target(config) != "parity":
        return
    state = parity_session(config)
    scenario = scenario_id(item)
    runs = state.runs.get(scenario)
    if runs is None or runs.real is None or runs.fake is None:
        return
    comparisons = compare_logs(
        scenario, runs.real, runs.fake, runs.spec, state.recorded_keys(runs.spec)
    )
    _report(state, config).add(scenario, comparisons)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write the parity report and enforce the threshold."""
    config = session.config
    if _target(config) != "parity":
        return
    state = parity_session(config)
    if not state.runs:
        return
    report = _report(state, config)
    report.unmatched = [
        scenario for scenario, runs in state.runs.items() if runs.real is None or runs.fake is None
    ]
    spec = next(iter(state.runs.values())).spec
    state.paths = report.write(spec.reports_dir or REPORTS_DIR, spec.model)
    if not report.ok and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    """Print the overall compliance."""
    del exitstatus
    state = config.stash.get(_SESSION, None)
    if _target(config) != "parity" or state is None or state.report is None or not state.paths:
        return
    report = state.report
    terminalreporter.section("fauxcall parity")
    verdict = "PASS" if report.ok else "FAIL"
    terminalreporter.write_line(
        f"{verdict}: compliance {report.score:.2%} (threshold {report.threshold:.2%}), "
        f"{len(report.scenarios)} scenarios compared"
    )
    if report.unmatched:
        terminalreporter.write_line(
            f"{len(report.unmatched)} scenarios ran on one side only: "
            + ", ".join(report.unmatched)
        )
    counts = report.call_counts()
    if counts["independent"] < counts["compared"]:
        terminalreporter.write_line(
            f"independent of recordings: {report.independent_score:.2%} over "
            f"{counts['independent']} of {counts['compared']} calls"
        )
    spec = next(iter(state.runs.values())).spec
    missing = report.not_covered(spec.model)
    if missing:
        terminalreporter.write_line(f"not covered by any scenario: {', '.join(missing)}")
    if report.refusal_checked(spec.model) is False:
        terminalreporter.write_line("no scenario checks a refused credential (401/403)")
    terminalreporter.write_line(f"report: {state.paths[1]}")
