"""Settings of this fake: ``FAUXCALL_*`` variables, then ``.env``, then ``fauxcall.toml``."""

from __future__ import annotations

from pathlib import Path

from fauxcall.config import FakeSettings
from pydantic_settings import SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
"""This fake's folder (holds ``data/``, ``.env`` and ``fauxcall.toml``)."""


class Settings(FakeSettings):
    """Runtime settings; see ``fauxcall.config.FakeSettings`` for every option."""

    model_config = SettingsConfigDict(
        env_prefix="FAUXCALL_",
        env_nested_delimiter="__",
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        toml_file=ROOT / "fauxcall.toml",
        extra="ignore",
        populate_by_name=True,
    )

    name: str = "everything"
    port: int = 8701
    data_dir: Path = ROOT / "data"
