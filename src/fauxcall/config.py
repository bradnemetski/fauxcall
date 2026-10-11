"""Configuration for the FauxCall CLI and for generated fakes, built on pydantic-settings.

Precedence (highest first): explicit arguments, environment variables (``FAUXCALL_*``, nested with
``__``), the ``.env`` file, ``fauxcall.toml``, then defaults. Generated fakes subclass
:class:`FakeSettings` and point ``env_file``/``toml_file``/``data_dir`` at their own directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, Literal

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource

from fauxcall import __version__
from fauxcall.logs import LogFormat, LogLevel

if TYPE_CHECKING:
    from pydantic_settings import PydanticBaseSettingsSource

DEFAULT_JEV_MODEL: Final = "jev-1.13.0"
"""Pinned Jev model: aliases like ``jev-latest`` move silently, which would break repeatability."""

DEFAULT_ADMIN_PREFIX: Final = "/__fauxcall"
DEFAULT_SESSION_HEADER: Final = "X-Fauxcall-Session"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClockSettings(_Section):
    """How a fake tells time. ``real`` is the default; the others make evaluations repeatable."""

    mode: Literal["real", "frozen", "offset", "scaled"] = "real"
    at: AwareDatetime | None = Field(
        default=None, description="Instant for 'frozen', start instant for 'scaled'."
    )
    offset_seconds: float = Field(default=0.0, description="Shift applied in 'offset' mode.")
    rate: float = Field(
        default=1.0, gt=0, description="Simulated seconds per real second in 'scaled' mode."
    )


IdentityStatus = Literal["allowed", "denied", "expired", "insufficient_scope"]


class IdentityConfig(_Section):
    """A user the fake knows about: credentials identify them, ``status`` decides the outcome."""

    name: str
    status: IdentityStatus = "allowed"
    api_key: SecretStr | None = None
    bearer_token: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    scopes: list[str] = Field(default_factory=list)


class AuthSettings(_Section):
    """Which credentials a fake accepts.

    ``any``: any well-formed credential is accepted unless it belongs to a configured user whose
    status says otherwise. ``allowlist``: only configured users get in. ``off``: no auth checks.
    """

    mode: Literal["any", "allowlist", "off"] = "any"
    users: list[IdentityConfig] = Field(default_factory=list)
    default_user: str | None = Field(
        default=None, description="Identity the built-in OAuth server approves when none is hinted."
    )


FaultKind = Literal["error", "timeout", "delay", "disconnect", "rate_limit"]


class FaultRule(_Section):
    """Inject a failure into matching calls, deterministically (seeded)."""

    endpoint: str = Field(default="*", description="Glob on the endpoint id, e.g. 'GET /pets/*'.")
    kind: FaultKind = "error"
    probability: float = Field(default=1.0, ge=0.0, le=1.0)
    status: int = Field(default=500, ge=100, le=599)
    error_template: str | None = Field(
        default=None, description="Named error template from the API model to render."
    )
    delay_ms: int = Field(default=0, ge=0)
    first_n: int | None = Field(default=None, ge=1, description="Only the first N matching calls.")
    every_nth: int | None = Field(default=None, ge=1, description="Only every Nth matching call.")
    session: str | None = Field(default=None, description="Only calls from this session.")


class RealTargetSettings(_Section):
    """Where the real API lives and how to authenticate, for recording and parity tests."""

    base_url: str | None = None
    headers: dict[str, SecretStr] = Field(default_factory=dict)
    query: dict[str, SecretStr] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30.0, gt=0)


class JevSettings(_Section):
    """Runtime use of Jev (TypeSafe). Only active when an API key is configured."""

    enabled: bool = True
    model: str = DEFAULT_JEV_MODEL
    timeout_seconds: float = Field(default=5.0, gt=0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class _FauxcallSettings(BaseSettings):
    toml_file_name: ClassVar[str] = "fauxcall.toml"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Add ``fauxcall.toml`` below environment variables and ``.env``."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


class FakeSettings(_FauxcallSettings):
    """Runtime settings for a generated fake. Every field can be set as ``FAUXCALL_<NAME>``."""

    model_config = SettingsConfigDict(
        env_prefix="FAUXCALL_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        toml_file="fauxcall.toml",
        extra="ignore",
        populate_by_name=True,
    )

    name: str = "fake"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=0, le=65535)
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "auto"
    data_dir: Path = Path("data")
    fast: bool = Field(default=False, description="Disable latency and rate-limit emulation.")
    latency: bool = True
    rate_limits: bool = True
    seed: int = 0
    clock: ClockSettings = Field(default_factory=ClockSettings)
    persist_state: bool = False
    state_dir: Path | None = None
    admin_prefix: str = DEFAULT_ADMIN_PREFIX
    admin_token: SecretStr | None = None
    session_header: str = DEFAULT_SESSION_HEADER
    journal_limit: int = Field(default=10_000, ge=0)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    faults: list[FaultRule] = Field(default_factory=list)
    jev: JevSettings = Field(default_factory=JevSettings)
    typesafe_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TYPESAFE_API_KEY", "FAUXCALL_TYPESAFE_API_KEY", "typesafe_api_key"
        ),
    )
    pseudonym_key: SecretStr | None = None
    real: RealTargetSettings = Field(default_factory=RealTargetSettings)

    @property
    def latency_enabled(self) -> bool:
        """Latency emulation is on unless disabled or in ``fast`` mode."""
        return self.latency and not self.fast

    @property
    def rate_limits_enabled(self) -> bool:
        """Rate-limit emulation is on unless disabled or in ``fast`` mode."""
        return self.rate_limits and not self.fast

    @property
    def jev_active(self) -> bool:
        """Jev is used only when enabled *and* a key is configured."""
        return self.jev.enabled and self.typesafe_api_key is not None

    @property
    def resolved_state_dir(self) -> Path:
        """Directory for opt-in persisted state (gitignored)."""
        return self.state_dir if self.state_dir is not None else self.data_dir / "state"


class CliSettings(_FauxcallSettings):
    """Settings for the ``fauxcall`` command-line tool."""

    model_config = SettingsConfigDict(
        env_prefix="FAUXCALL_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        toml_file="fauxcall.toml",
        extra="ignore",
        populate_by_name=True,
    )

    workspace: Path = Field(default=Path("fauxcall"), description="Where fakes are generated.")
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "auto"
    user_agent: str = f"fauxcall/{__version__} (+https://github.com/bradnemetski/fauxcall)"
    record_rps: float = Field(default=2.0, gt=0, description="Max requests/second to real APIs.")
    record_max_calls: int = Field(default=2000, ge=1, description="Budget per recording run.")
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    source: str | None = Field(
        default=None,
        description=(
            "Where generated fakes get the fauxcall package from: a local checkout path "
            "(development) or a version specifier. Defaults to this CLI's version."
        ),
    )
    typesafe_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TYPESAFE_API_KEY", "FAUXCALL_TYPESAFE_API_KEY", "typesafe_api_key"
        ),
    )
