"""Helpers shared by the CLI commands."""

from __future__ import annotations

import asyncio
import functools
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx2
import typer
from pydantic_settings import SettingsConfigDict

from fauxcall.config import CliSettings, FakeSettings
from fauxcall.errors import ConfigurationError, FauxcallError
from fauxcall.model.api import ApiModel
from fauxcall.model.progress import Progress
from fauxcall.paths import FakeLayout, Workspace
from fauxcall.storage import read_model, write_model

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from pydantic import SecretStr

EXIT_ERROR: Final = 1
CLEAN_ERRORS: Final = (FauxcallError, ValueError, OSError, httpx2.HTTPError)
"""Failures reported as one ``error: …`` line instead of a traceback."""


def cli_settings() -> CliSettings:
    """Settings for this CLI invocation."""
    return CliSettings()


def workspace() -> Workspace:
    """The configured workspace (``./fauxcall`` by default)."""
    return Workspace(cli_settings().workspace)


def layout(name: str) -> FakeLayout:
    """An existing fake, or a clear error."""
    found = workspace().fake(name)
    if not found.exists():
        known = ", ".join(f.name for f in workspace().fakes()) or "none yet"
        raise ConfigurationError(f"no fake called {name!r} in {workspace().root} (known: {known})")
    return found


def load_model(fake: FakeLayout) -> ApiModel:
    """The fake's model."""
    return read_model(fake.model_path, ApiModel)


def save_model(fake: FakeLayout, model: ApiModel) -> list[str]:
    """Write the model; returns consistency problems (written anyway so work isn't lost)."""
    write_model(fake.model_path, model)
    return model.problems()


def load_progress(fake: FakeLayout) -> Progress:
    """The fake's checkpoints."""
    return read_model(fake.progress_path, Progress)


def save_progress(fake: FakeLayout, progress: Progress) -> None:
    """Write checkpoints with a fresh ``updated_at``."""
    progress.updated_at = now()
    write_model(fake.progress_path, progress)


def now() -> datetime:
    """Current UTC time."""
    return datetime.now(UTC)


def fake_settings(fake: FakeLayout, **overrides: Any) -> FakeSettings:  # noqa: ANN401
    """Runtime settings of a fake, read from *its* ``.env`` and ``fauxcall.toml``."""

    class _Settings(FakeSettings):
        model_config = SettingsConfigDict(
            env_prefix="FAUXCALL_",
            env_nested_delimiter="__",
            env_file=fake.env_file,
            env_file_encoding="utf-8",
            toml_file=fake.root / "fauxcall.toml",
            extra="ignore",
            populate_by_name=True,
        )

    overrides.setdefault("data_dir", fake.data)
    overrides.setdefault("name", fake.name)
    return _Settings(**overrides)


def secret_values(settings: FakeSettings) -> list[str]:
    """Every credential the settings hold, for the recorder's privacy layer.

    The real API's headers and query parameters, each configured user's API key, bearer token and
    password, and the Jev key. A value with an auth scheme (``Bearer abc``) contributes both the
    full value and the part after the scheme, so the bare token is scrubbed wherever it appears.
    """
    secrets: list[SecretStr | None] = [
        *settings.real.headers.values(),
        *settings.real.query.values(),
    ]
    for user in settings.auth.users:
        secrets += [user.api_key, user.bearer_token, user.password]
    secrets.append(settings.typesafe_api_key)
    values: list[str] = []
    for secret in secrets:
        value = secret.get_secret_value() if secret is not None else ""
        if not value:
            continue
        values.append(value)
        _, _, credential = value.strip().partition(" ")
        if credential.strip():
            values.append(credential.strip())
    return list(dict.fromkeys(values))


def http_client(*, follow_redirects: bool = True) -> httpx2.AsyncClient:
    """HTTP client for discovery and recording."""
    settings = cli_settings()
    return httpx2.AsyncClient(
        headers={"user-agent": settings.user_agent},
        timeout=settings.request_timeout_seconds,
        follow_redirects=follow_redirects,
    )


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion."""
    return asyncio.run(coroutine)


def fail(message: str) -> typer.Exit:
    """Print an error and return an exit to raise."""
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    return typer.Exit(EXIT_ERROR)


def clean_errors[**P, R](command: Callable[P, R]) -> Callable[P, R]:
    """Decorate a command so expected failures end with ``error: …`` and exit status 1.

    The signature is preserved (``functools.wraps``), so Typer still sees every parameter.
    """

    @functools.wraps(command)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return command(*args, **kwargs)
        except CLEAN_ERRORS as error:
            raise fail(str(error)) from error

    return wrapper


def echo(text: str = "") -> None:
    """Print to stdout."""
    typer.echo(text)
