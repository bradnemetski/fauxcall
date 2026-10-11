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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx2
import pytest
import pytest_asyncio

from fauxcall.mcpwire.client import McpWireClient
from fauxcall.testing.client import (
    ApiClient,
    ExchangeLog,
    McpClient,
    placeholder_credentials,
    with_query,
)
from fauxcall.testing.compare import W_STATUS, Comparison, compare
from fauxcall.testing.report import ParityReport, endpoint_for, endpoint_matcher

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from fastapi import FastAPI

    from fauxcall.model.api import ApiModel
    from fauxcall.model.endpoints import EndpointModel
    from fauxcall.testing.client import Target
    from fauxcall.testing.compare import Translate

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
    translate: Translate | None = None
    mcp_path: str = "/mcp"
    real_transport: httpx2.AsyncBaseTransport | None = None
    """Reach the real side through this transport (an in-process app …) rather than the network."""


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
    transport = httpx2.ASGITransport(app=fake.app_factory())
    client = httpx2.AsyncClient(transport=transport, base_url=FAKE_BASE_URL, **options)
    return client, credentials.query


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
    client = ApiClient(
        target,
        http,
        allow_writes=bool(request.config.getoption("--allow-real-writes")),
        translate=fauxcall_fake.translate,
        query=query,
        log=fauxcall_log,
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

    def mutating(name: str) -> bool:
        tool = model.mcp.tool(name) if model is not None and model.mcp is not None else None
        return tool_is_mutating(tool.definition if tool else None)

    async with http:
        client = McpClient(
            target,
            McpWireClient(url, http),
            allow_writes=bool(request.config.getoption("--allow-real-writes")),
            mutating=mutating,
            log=fauxcall_log,
        )
        try:
            yield client
        finally:
            await client.close()
    _keep(request, target, fauxcall_log, fauxcall_fake)


def record_roots(endpoint: EndpointModel) -> list[str] | None:
    """Where an endpoint's records sit in its responses (``None``: let the comparison guess)."""
    if endpoint.role in _LIST_ROLES:
        if endpoint.shape.kind == "envelope":
            return [f"{endpoint.shape.items_path or '$.data'}[*]"]
        return ["$[*]"]
    if endpoint.role in _RECORD_ROLES:
        return ["$"]
    return None


def compare_logs(
    scenario: str, real: ExchangeLog, fake: ExchangeLog, fake_spec: FakeUnderTest
) -> list[Comparison]:
    """Compare two runs of the same scenario call by call."""
    model = fake_spec.model
    matchers = endpoint_matcher(model)
    comparisons: list[Comparison] = []
    for index, real_call in enumerate(real.calls):
        label = f"{real_call.method} {real_call.path}"
        if index >= len(fake.calls):
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
        comparisons.append(
            compare(
                label,
                real_call.observation,
                fake.calls[index].observation,
                policies=[*(endpoint.fields if endpoint else []), *options.policies],
                translate=fake_spec.translate,
                compare_lengths=options.lengths,
                max_value_checks=200 if options.values else 0,
                record_roots=record_roots(endpoint) if endpoint else None,
            )
        )
    return comparisons


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
    _report(state, config).add(scenario, compare_logs(scenario, runs.real, runs.fake, runs.spec))


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
    terminalreporter.write_line(f"report: {state.paths[1]}")
