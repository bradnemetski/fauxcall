"""CLI: serve, up, down, ps and verify (processes, sockets and uvicorn are mocked)."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx2
import pytest
import uvicorn
from typer.testing import CliRunner

from fauxcall.cli import running
from fauxcall.cli.main import app
from fauxcall.model.api import ApiModel, RealTarget, SourceInfo
from fauxcall.model.endpoints import EndpointModel
from fauxcall.model.mcp import McpModel, McpTransportModel
from fauxcall.model.progress import Progress
from fauxcall.paths import Workspace
from fauxcall.storage import write_model
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from typer.testing import Result

    from fauxcall.paths import FakeLayout


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    for variable in list(os.environ):
        if variable.startswith("FAUXCALL_") or variable.endswith("TYPESAFE_API_KEY"):
            monkeypatch.delenv(variable)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAUXCALL_WORKSPACE", str(tmp_path / "fakes"))
    return Workspace(tmp_path / "fakes")


def invoke(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == code, (result.output, result.exception)
    return result


def rest_model(name: str = "shop") -> ApiModel:
    return ApiModel(
        name=name,
        title="Shop",
        source=SourceInfo(
            kind="openapi", url="https://shop.test/openapi.json", fetched_at=datetime.now(UTC)
        ),
        real=RealTarget(base_url="https://shop.test"),
        endpoints=[EndpointModel(id="GET /items", method=HttpMethod.GET, path="/items")],
    )


def mcp_model(name: str = "demo") -> ApiModel:
    return ApiModel(
        name=name,
        title="Demo",
        kind="mcp",
        source=SourceInfo(kind="mcp", url="http://mcp.test/v1/mcp", fetched_at=datetime.now(UTC)),
        real=RealTarget(base_url="http://mcp.test"),
        mcp=McpModel(transport=McpTransportModel(path="/v1/mcp")),
    )


def make_fake(ws: Workspace, model: ApiModel, *, port: int | None = None) -> FakeLayout:
    fake = ws.fake(model.name)
    write_model(fake.model_path, model)
    now = datetime.now(UTC)
    progress = Progress(fake=model.name, source=model.source.url, started_at=now, updated_at=now)
    write_model(fake.progress_path, progress)
    if port is not None:
        (fake.root / "fauxcall.toml").write_text(f'name = "{model.name}"\nport = {port}\n')
    return fake


@dataclass
class Processes:
    """Records what ``up``/``down`` do to processes instead of doing it."""

    started: list[list[str]] = field(default_factory=list)
    alive: set[int] = field(default_factory=set)
    killed: list[tuple[int, int]] = field(default_factory=list)
    listening: bool = True
    next_pid: int = 4242


@pytest.fixture
def processes(monkeypatch: pytest.MonkeyPatch) -> Processes:
    state = Processes()

    @dataclass
    class FakePopen:
        pid: int

    def popen(command: list[str], **_kwargs: object) -> FakePopen:
        state.started.append(command)
        state.next_pid += 1
        state.alive.add(state.next_pid)
        return FakePopen(state.next_pid)

    def kill(pid: int, sig: int) -> None:
        assert sig == 0
        if pid not in state.alive:
            raise ProcessLookupError(pid)

    def killpg(group: int, sig: int) -> None:
        state.killed.append((group, sig))
        state.alive.discard(group)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(running, "_listening", lambda _port: state.listening)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    return state


def run_file(ws: Workspace) -> dict[str, dict[str, object]]:
    data: dict[str, dict[str, object]] = json.loads((ws.root / ".fauxcall/run.json").read_text())
    return data


# -- serve ----------------------------------------------------------------------------------------


def test_serve_builds_the_app_from_the_model(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_fake(ws, rest_model(), port=8765)
    served: list[tuple[FastAPI, dict[str, object]]] = []

    def run(application: FastAPI, **options: object) -> None:
        served.append((application, options))

    monkeypatch.setattr(uvicorn, "run", run)
    assert invoke("serve", "shop").output.strip() == "serving shop on http://127.0.0.1:8765"
    invoke("serve", "shop", "--port", "9001", "--host", "0.0.0.0", "--fast")  # noqa: S104
    (application, options), (fast_app, fast_options) = served
    assert options == {"host": "127.0.0.1", "port": 8765, "log_config": None, "access_log": False}
    assert fast_options["port"] == 9001
    assert fast_options["host"] == "0.0.0.0"  # noqa: S104
    assert fast_app.state.fauxcall.settings.fast is True
    assert application.state.fauxcall.settings.fast is False


async def test_served_app_answers(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    make_fake(ws, rest_model())
    served: list[FastAPI] = []
    monkeypatch.setattr(uvicorn, "run", lambda application, **_options: served.append(application))
    invoke("serve", "shop", "--fast")
    transport = httpx2.ASGITransport(app=served[0])
    async with httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client:
        health = await client.get("/__fauxcall/health")
        await client.get("/items")
        journal = (await client.get("/__fauxcall/journal")).json()
    assert health.status_code == 200
    assert [entry["endpoint"] for entry in journal["entries"]] == ["GET /items"]


def test_serve_unknown_fake(ws: Workspace) -> None:
    assert "no fake called 'nope'" in invoke("serve", "nope", code=1).output


# -- up / down / ps -------------------------------------------------------------------------------


def test_up_starts_every_fake_and_prints_mcp_config(ws: Workspace, processes: Processes) -> None:
    make_fake(ws, rest_model(), port=8701)
    make_fake(ws, mcp_model(), port=8702)
    output = invoke("up", "--fast").output
    assert "demo: up on http://127.0.0.1:8702" in output
    assert "shop: up on http://127.0.0.1:8701" in output
    config = json.loads(output.split("MCP config (e.g. .mcp.json):\n", 1)[1])
    assert config == {
        "mcpServers": {"demo": {"type": "http", "url": "http://127.0.0.1:8702/v1/mcp"}}
    }
    assert [command[-4:] for command in processes.started] == [
        ["demo", "--port", "8702", "--fast"],
        ["shop", "--port", "8701", "--fast"],
    ]
    entries = run_file(ws)
    assert set(entries) == {"demo", "shop"}
    assert entries["shop"]["port"] == 8701
    assert str(entries["shop"]["log"]).endswith(".fauxcall/shop.log")


def test_up_skips_fakes_already_running(ws: Workspace, processes: Processes) -> None:
    make_fake(ws, rest_model(), port=8701)
    invoke("up", "shop")
    assert invoke("up", "shop").output.strip() == "shop: already running on http://127.0.0.1:8701"
    assert len(processes.started) == 1
    assert "--fast" not in processes.started[0]


def test_up_reports_fakes_still_starting(ws: Workspace, processes: Processes) -> None:
    make_fake(ws, rest_model(), port=8701)
    processes.listening = False
    output = invoke("up", "shop", "--wait", "0").output
    assert "shop: starting (see " in output


def test_up_waits_until_the_fake_listens(
    ws: Workspace, processes: Processes, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_fake(ws, rest_model(), port=8701)
    answers = iter([False, False, True, True])
    naps: list[float] = []
    monkeypatch.setattr(running, "_listening", lambda _port: next(answers))
    monkeypatch.setattr(time, "sleep", naps.append)
    assert invoke("up", "shop").output.strip() == "shop: up on http://127.0.0.1:8701"
    assert naps == [0.2, 0.2]


def test_up_needs_fakes(ws: Workspace, processes: Processes) -> None:
    assert "no fakes to start" in invoke("up", code=1).output
    assert "no fake called 'ghost'" in invoke("up", "ghost", code=1).output


def test_down_stops_fakes(ws: Workspace, processes: Processes) -> None:
    make_fake(ws, rest_model(), port=8701)
    make_fake(ws, mcp_model(), port=8702)
    invoke("up")
    shop_pid = int(str(run_file(ws)["shop"]["pid"]))
    output = invoke("down", "shop", "nope").output
    assert "shop: stopped" in output
    assert "nope: not running" in output
    assert processes.killed == [(shop_pid, signal.SIGTERM)]
    assert set(run_file(ws)) == {"demo"}
    processes.alive.clear()
    assert invoke("down").output.strip() == "demo: stopped"
    assert run_file(ws) == {}


def test_down_tolerates_processes_that_vanish(
    ws: Workspace, processes: Processes, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_fake(ws, rest_model(), port=8701)
    invoke("up")

    def gone(_pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(os, "getpgid", gone)
    assert invoke("down").output.strip() == "shop: stopped"


def test_ps(ws: Workspace, processes: Processes) -> None:
    assert invoke("ps").output.strip() == "nothing running"
    make_fake(ws, rest_model(), port=8701)
    invoke("up")
    pid = run_file(ws)["shop"]["pid"]
    assert invoke("ps").output.strip() == f"shop: up (pid {pid}) http://127.0.0.1:8701"
    processes.listening = False
    assert invoke("ps").output.strip() == f"shop: down (pid {pid}) http://127.0.0.1:8701"


def test_run_file_tolerates_damage(ws: Workspace, processes: Processes) -> None:
    run = ws.root / ".fauxcall" / "run.json"
    run.parent.mkdir(parents=True)
    run.write_text(
        json.dumps({"bad": {"pid": "x"}, "partial": {"port": 1}, "ok": {"pid": 7, "port": 8}})
    )
    assert invoke("ps").output.strip() == "ok: down (pid 7) http://127.0.0.1:8"
    run.write_text("[]")
    assert invoke("ps").output.strip() == "nothing running"


def test_alive_and_listening_probes() -> None:
    assert running._alive(os.getpid()) is True
    assert running._alive(2**22 + 12345) is False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert running._listening(port) is True
    assert running._listening(port) is False


# -- verify ---------------------------------------------------------------------------------------


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, object]]]:
    seen: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        seen.append((command, options))
        return subprocess.CompletedProcess(command, 3 if "parity" in command else 0)

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_verify_runs_the_fake_suite_with_coverage(
    ws: Workspace, commands: list[tuple[list[str], dict[str, object]]]
) -> None:
    fake = make_fake(ws, rest_model())
    (fake.root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    invoke("verify", "shop")
    [(command, options)] = commands
    assert command == [
        "uv",
        "run",
        "--project",
        str(fake.root),
        "pytest",
        "--target",
        "fake",
        "--cov",
        "--cov-report=term-missing",
    ]
    assert options == {"cwd": fake.root, "check": False}
    invoke("verify", "shop", "--e2e-only", "--allow-real-writes")
    assert commands[1][0][-2:] == [str(fake.tests / "e2e"), "--allow-real-writes"]


def test_verify_from_the_project_root_with_a_relative_workspace(
    ws: Workspace,
    commands: list[tuple[list[str], dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = make_fake(ws, rest_model())
    (fake.root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    monkeypatch.setenv("FAUXCALL_WORKSPACE", "fakes")  # relative paths, run from another folder
    invoke("verify", "shop", "--e2e-only")
    [(command, options)] = commands
    assert command[3] == str(fake.root.resolve())
    assert command[-1] == str(fake.tests.resolve() / "e2e")
    assert options["cwd"] == fake.root.resolve()


def test_verify_parity_prints_only_the_report_it_wrote(
    ws: Workspace,
    commands: list[tuple[list[str], dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = make_fake(ws, rest_model())
    (fake.root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    result = invoke("verify", "shop", "--target", "parity", code=3)
    assert "This run wrote no parity report" in result.output
    fake.reports.mkdir(parents=True)
    (fake.reports / "parity.md").write_text("# Parity report — PASS (an old run)\n")
    result = invoke("verify", "shop", "--target", "parity", code=3)
    assert "an old run" not in result.output  # a stale pass is never shown as this run's
    assert "This run wrote no parity report" in result.output
    assert commands[-1][0][-3:] == ["--target", "parity", str(fake.tests / "e2e")]

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        commands.append((command, options))
        (fake.reports / "parity.md").write_text("# Parity report — FAIL\n")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(subprocess, "run", run)
    assert "# Parity report — FAIL" in invoke("verify", "shop", "--target", "parity", code=1).output
    invoke("verify", "shop", "--target", "real", code=1)
    assert commands[-1][0][-1] == str(fake.tests / "e2e")


def test_verify_fails_clearly_when_no_tests_ran(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_fake(ws, rest_model())
    (fake.root / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 5)

    monkeypatch.setattr(subprocess, "run", run)
    assert "no tests ran" in invoke("verify", "shop", "--target", "parity", code=1).output


def test_verify_errors(ws: Workspace, commands: list[tuple[list[str], dict[str, object]]]) -> None:
    make_fake(ws, rest_model())
    assert "unknown target 'prod'" in invoke("verify", "shop", "--target", "prod", code=1).output
    assert "scaffold the fake first" in invoke("verify", "shop", code=1).output
    assert commands == []
