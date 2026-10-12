from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from fauxcall.config import (
    DEFAULT_JEV_MODEL,
    CliSettings,
    ClockSettings,
    FakeSettings,
    FaultRule,
    IdentityConfig,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("TYPESAFE_API_KEY", "FAUXCALL_PORT", "FAUXCALL_FAST"):
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    settings = FakeSettings()
    assert settings.port == 8000
    assert settings.clock.mode == "real"
    assert settings.latency_enabled
    assert settings.rate_limits_enabled
    assert not settings.jev_active
    assert settings.jev.model == DEFAULT_JEV_MODEL
    assert settings.resolved_state_dir == Path("data/state")
    assert settings.admin_prefix == "/__fauxcall"


def test_fast_mode_disables_realism() -> None:
    settings = FakeSettings(fast=True)
    assert not settings.latency_enabled
    assert not settings.rate_limits_enabled
    assert FakeSettings(latency=False).rate_limits_enabled
    assert FakeSettings(state_dir=Path("/tmp/x")).resolved_state_dir == Path("/tmp/x")  # noqa: S108


def test_environment_nested_values_and_jev_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAUXCALL_PORT", "9100")
    monkeypatch.setenv("FAUXCALL_CLOCK__MODE", "frozen")
    monkeypatch.setenv("FAUXCALL_CLOCK__AT", "2026-01-05T15:30:00Z")
    monkeypatch.setenv(
        "FAUXCALL_AUTH__USERS", '[{"name": "mallory", "status": "denied", "api_key": "k-1"}]'
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-secret")
    settings = FakeSettings()
    assert settings.port == 9100
    assert settings.clock.mode == "frozen"
    assert settings.clock.at is not None
    assert settings.auth.users[0].status == "denied"
    assert settings.auth.users[0].api_key is not None
    assert settings.auth.users[0].api_key.get_secret_value() == "k-1"
    assert settings.jev_active
    assert "ts-secret" not in repr(settings)


def test_dotenv_and_toml_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "fauxcall.toml").write_text(
        'name = "from-toml"\nport = 7000\nseed = 3\n'
        '[[faults]]\nendpoint = "GET /pets/*"\nprobability = 0.5\n'
    )
    (tmp_path / ".env").write_text("FAUXCALL_PORT=7100\n")
    settings = FakeSettings()
    assert settings.name == "from-toml"
    assert settings.seed == 3
    assert settings.port == 7100  # .env beats TOML
    assert settings.faults == [FaultRule(endpoint="GET /pets/*", probability=0.5)]
    monkeypatch.setenv("FAUXCALL_PORT", "7200")
    assert FakeSettings().port == 7200  # environment beats .env


def test_validation_errors() -> None:
    with pytest.raises(ValidationError):
        FakeSettings(port=70000)
    with pytest.raises(ValidationError):
        ClockSettings(rate=0)
    with pytest.raises(ValidationError):
        ClockSettings.model_validate({"mode": "frozen", "unknown": 1})
    with pytest.raises(ValidationError):
        FaultRule(probability=1.5)
    with pytest.raises(ValidationError):
        ClockSettings.model_validate({"at": "2026-01-05T15:30:00"})  # must be timezone-aware


def test_log_levels_in_any_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAUXCALL_LOG_LEVEL", "info")
    assert FakeSettings().log_level == "INFO"
    assert CliSettings().log_level == "INFO"
    with pytest.raises(ValidationError):
        FakeSettings.model_validate({"log_level": "loud"})


def test_identity_defaults() -> None:
    identity = IdentityConfig(name="alice")
    assert identity.status == "allowed"
    assert identity.scopes == []


def test_cli_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = CliSettings()
    assert settings.workspace == Path("fauxcall")
    assert settings.user_agent.startswith("fauxcall/")
    monkeypatch.setenv("FAUXCALL_WORKSPACE", "fakes")
    monkeypatch.setenv("FAUXCALL_SOURCE", "/src/fauxcall")
    settings = CliSettings()
    assert settings.workspace == Path("fakes")
    assert settings.source == "/src/fauxcall"
