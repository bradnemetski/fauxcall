"""CLI: run fakes (serve, up, down, ps) and verify them (verify)."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

import typer

from fauxcall.build.scaffold import mcp_server_config
from fauxcall.cli.common import (
    clean_errors,
    echo,
    fail,
    fake_settings,
    layout,
    load_model,
    workspace,
)
from fauxcall.cli.evidence import apply_answers
from fauxcall.storage import atomic_write_text

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.paths import FakeLayout

TARGETS: Final = ("fake", "real", "parity")


class RunEntry(TypedDict):
    """A fake started by ``up`` (``<workspace>/.fauxcall/run.json``)."""

    pid: int
    port: int
    log: str


def _port(fake: FakeLayout) -> int:
    return fake_settings(fake).port


@clean_errors
def serve(
    name: str,
    port: Annotated[int | None, typer.Option()] = None,
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    fast: Annotated[
        bool, typer.Option("--fast", help="No latency or rate-limit emulation.")
    ] = False,
) -> None:
    """Serve a fake in the foreground.

    A scaffolded fake runs its own code (its handlers, routes and settings); before scaffolding,
    FauxCall's engine serves the routes the model describes.
    """
    import uvicorn  # noqa: PLC0415

    from fauxcall.runtime.app import create_app  # noqa: PLC0415
    from fauxcall.runtime.dynamic import dynamic_router  # noqa: PLC0415

    fake = layout(name)
    if _scaffolded(fake):
        raise typer.Exit(_serve_own_code(fake, port=port, host=host, fast=fast))
    overrides: dict[str, object] = {"host": host, "fast": fast}
    if port is not None:
        overrides["port"] = port
    settings = fake_settings(fake, **overrides)
    model = load_model(fake)
    app = create_app(settings, routers=[dynamic_router(model)])
    echo(f"serving {name} on http://{settings.host}:{settings.port}")
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, access_log=False)


def _scaffolded(fake: FakeLayout) -> bool:
    """Whether the fake has its own project and entry point (``fauxcall scaffold`` ran)."""
    return (fake.root / "pyproject.toml").exists() and (fake.src / "__main__.py").exists()


def _serve_own_code(fake: FakeLayout, *, port: int | None, host: str, fast: bool) -> int:
    """Run the fake's own command in its own environment; returns its exit code."""
    root = fake.root.resolve()
    environment = _own_environment() | {
        "FAUXCALL_HOST": host,
        "FAUXCALL_FAST": "true" if fast else "false",
    }
    if port is not None:
        environment["FAUXCALL_PORT"] = str(port)
    settings = fake_settings(fake, host=host, **({"port": port} if port is not None else {}))
    echo(f"serving {fake.name} (its own code) on http://{settings.host}:{settings.port}")
    command = ["uv", "run", "--project", str(root), f"fauxcall-{fake.name}"]
    return subprocess.run(command, cwd=root, env=environment, check=False).returncode  # noqa: S603


def _run_file() -> Path:
    return workspace().root / ".fauxcall" / "run.json"


def _read_run() -> dict[str, RunEntry]:
    path = _run_file()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    entries: dict[str, RunEntry] = {}
    for name, entry in raw.items() if isinstance(raw, dict) else ():
        with contextlib.suppress(KeyError, TypeError, ValueError):
            entries[name] = RunEntry(
                pid=int(entry["pid"]), port=int(entry["port"]), log=str(entry.get("log", ""))
            )
    return entries


def _write_run(running: dict[str, RunEntry]) -> None:
    atomic_write_text(_run_file(), json.dumps(running, indent=2) + "\n")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _free_port(preferred: int, busy: set[int]) -> int:
    """``preferred``, or the next port no started fake holds and nothing listens on.

    Gives up after ``_PORT_SEARCH`` ports and returns ``preferred`` (the start then says why).
    """
    candidates = range(preferred, preferred + _PORT_SEARCH)
    return next((p for p in candidates if p not in busy and not _listening(p)), preferred)


def _start(fake: FakeLayout, port: int, *, fast: bool, wait: float) -> RunEntry:
    command = [sys.executable, "-m", "fauxcall", "serve", fake.name, "--port", str(port)]
    if fast:
        command.append("--fast")
    log_path = workspace().root / ".fauxcall" / f"{fake.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(  # noqa: S603 - our own interpreter and arguments
            command, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True
        )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and not _listening(port):
        time.sleep(0.2)
    state = "up" if _listening(port) else f"starting (see {log_path})"
    echo(f"{fake.name}: {state} on http://127.0.0.1:{port}")
    return RunEntry(pid=process.pid, port=port, log=str(log_path))


@clean_errors
def up(
    names: Annotated[
        list[str] | None, typer.Argument(help="Fakes to start (default: all).")
    ] = None,
    fast: Annotated[bool, typer.Option("--fast")] = False,
    wait: Annotated[float, typer.Option(help="Seconds to wait for each fake to listen.")] = 20.0,
) -> None:
    """Start fakes in the background and print how agents reach them."""
    fakes = [layout(n) for n in names] if names else workspace().fakes()
    if not fakes:
        raise fail("no fakes to start")
    running = _read_run()
    mcp_servers: dict[str, object] = {}
    for fake in fakes:
        entry = running.get(fake.name)
        if entry and _alive(entry["pid"]):
            echo(f"{fake.name}: already running on http://127.0.0.1:{entry['port']}")
        else:
            busy = {e["port"] for e in running.values() if _alive(e["pid"])}
            configured = _port(fake)
            port = _free_port(configured, busy)
            if port != configured:
                echo(f"{fake.name}: port {configured} is in use; starting on {port}")
            running[fake.name] = _start(fake, port, fast=fast, wait=wait)
        model = load_model(fake)
        if model.mcp is not None:
            mcp_servers[fake.name] = mcp_server_config(model, running[fake.name]["port"])
    _write_run(running)
    if mcp_servers:
        echo("\nMCP config (e.g. .mcp.json):\n" + json.dumps({"mcpServers": mcp_servers}, indent=2))


@clean_errors
def down(names: Annotated[list[str] | None, typer.Argument()] = None) -> None:
    """Stop fakes started with `up`."""
    running = _read_run()
    for name in list(names or running):
        entry = running.pop(name, None)
        if entry is None:
            echo(f"{name}: not running")
            continue
        pid = entry["pid"]
        if _alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        echo(f"{name}: stopped")
    _write_run(running)


@clean_errors
def ps() -> None:
    """Show which fakes are running."""
    running = _read_run()
    if not running:
        echo("nothing running")
        return
    for name, entry in running.items():
        pid, port = entry["pid"], entry["port"]
        state = "up" if _alive(pid) and _listening(port) else "down"
        echo(f"{name}: {state} (pid {pid}) http://127.0.0.1:{port}")


_NO_TESTS = 5  # pytest's exit code when it collected nothing
_PORT_SEARCH = 100


@clean_errors
def verify(
    name: str,
    target: Annotated[str, typer.Option(help="fake | real | parity")] = "fake",
    allow_real_writes: Annotated[bool, typer.Option("--allow-real-writes")] = False,
    e2e_only: Annotated[bool, typer.Option("--e2e-only")] = False,
    fake_url: Annotated[
        str | None,
        typer.Option(help="Test a running fake (e.g. one started with `fauxcall up`)."),
    ] = None,
) -> None:
    """Run the fake's test suite (and, with --target parity, the compliance gate)."""
    if target not in TARGETS:
        raise fail(f"unknown target {target!r}; one of: {', '.join(TARGETS)}")
    fake = layout(name)
    root = fake.root.resolve()  # the run happens inside the fake: relative paths would break
    if not (root / "pyproject.toml").exists():
        raise fail("scaffold the fake first (`fauxcall scaffold`)")
    if target != "fake":
        added = apply_answers(fake)  # a headless run's approval, for the real side's writes
        if added:
            echo(f"applied the write approval in answers.json: {', '.join(added)}")
    command = _pytest_command(
        fake, target, e2e_only=e2e_only, allow_real_writes=allow_real_writes, fake_url=fake_url
    )
    report = fake.reports.resolve() / "parity.md"
    before = _version_of(report)
    environment = _own_environment()
    completed = subprocess.run(command, cwd=root, env=environment, check=False)  # noqa: S603
    if completed.returncode == _NO_TESTS:
        raise fail("no tests ran: is the fake scaffolded, and are its tests in place?")
    if target == "parity":
        if (after := _version_of(report)) is not None and after != before:
            echo(report.read_text())
        else:
            echo("This run wrote no parity report (an earlier one is not reprinted).")
    raise typer.Exit(completed.returncode)


def _pytest_command(
    fake: FakeLayout, target: str, *, e2e_only: bool, allow_real_writes: bool, fake_url: str | None
) -> list[str]:
    """The fake's pytest run, in its own project."""
    command = ["uv", "run", "--project", str(fake.root.resolve()), "pytest", "--target", target]
    if e2e_only or target != "fake":
        command.append(str(fake.tests.resolve() / "e2e"))
    else:
        command += ["--cov", "--cov-report=term-missing"]
    if allow_real_writes:
        command.append("--allow-real-writes")
    if fake_url:
        command += ["--fake-url", fake_url]
    return command


def _own_environment() -> dict[str, str]:
    """The environment for a command in the fake's project.

    The fake has its own environment; an active one (the user's project's) only draws a warning.
    """
    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


def _version_of(path: Path) -> tuple[int, bytes] | None:
    """Enough to tell a rewritten file from the one before (mtimes alone can be coarse)."""
    return (path.stat().st_mtime_ns, path.read_bytes()) if path.exists() else None
