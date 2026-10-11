from __future__ import annotations

import logging
import runpy
from typing import TYPE_CHECKING, Any

import pytest
import uvicorn

from fauxcall_reference_api.logs import LOG_FORMAT, configure_logging, get_logger
from fauxcall_reference_api.main import build_parser, load_settings, main
from fauxcall_reference_api.settings import DEFAULT_API_KEYS, Settings

if TYPE_CHECKING:
    from collections.abc import Iterator

ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - only a settings value, nothing binds to it


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_run(app: object, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return calls


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HOST", "PORT", "LATENCY", "LOG_LEVEL", "API_KEYS"):
        monkeypatch.delenv(f"REFAPI_{name}", raising=False)
    settings = load_settings([])
    assert (settings.host, settings.port, settings.latency, settings.log_level) == (
        "127.0.0.1",
        9100,
        True,
        "INFO",
    )
    assert settings.api_keys == DEFAULT_API_KEYS
    assert (settings.rate_limit_requests, settings.rate_limit_window_seconds) == (30, 10)


def test_environment_and_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFAPI_PORT", "9200")
    monkeypatch.setenv("REFAPI_HOST", ALL_INTERFACES)
    monkeypatch.setenv("REFAPI_API_KEYS", '{"ci-key": "readonly"}')
    from_env = load_settings([])
    assert (from_env.port, from_env.host) == (9200, ALL_INTERFACES)
    assert from_env.api_keys == {"ci-key": "readonly"}
    overridden = load_settings(["--port", "9300", "--no-latency", "--log-level", "debug"])
    assert (overridden.port, overridden.latency, overridden.log_level) == (9300, False, "DEBUG")
    assert overridden.host == ALL_INTERFACES


def test_invalid_arguments_exit() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--port", "abc"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--log-level", "loud"])


def test_main_runs_uvicorn(uvicorn_calls: list[dict[str, Any]]) -> None:
    main(["--port", "9101", "--host", "127.0.0.1", "--no-latency", "--log-level", "WARNING"])
    (call,) = uvicorn_calls
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 9101
    assert call["log_config"] is None
    assert call["access_log"] is False
    assert call["app"].state.context.settings.latency is False
    assert logging.getLogger().level == logging.WARNING


def test_python_dash_m(
    uvicorn_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["fauxcall-reference-api", "--port", "9102"])
    runpy.run_module("fauxcall_reference_api", run_name="__main__")
    assert uvicorn_calls[0]["port"] == 9102


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])
    assert capsys.readouterr().out.strip() == "fauxcall-reference-api 1.0.0"


def test_configure_logging() -> None:
    configure_logging("DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    (handler,) = root.handlers
    assert handler.formatter is not None
    assert handler.formatter._fmt == LOG_FORMAT
    assert get_logger("access").name == "fauxcall_reference_api.access"


def test_settings_validation() -> None:
    with pytest.raises(ValueError, match="port"):
        Settings(port=70_000)
    with pytest.raises(ValueError, match="api_keys"):
        Settings(api_keys={"k": "admin"})  # type: ignore[dict-item]
