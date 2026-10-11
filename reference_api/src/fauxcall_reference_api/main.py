"""Command-line entry point: ``fauxcall-reference-api [--host H] [--port P] [--no-latency]``."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import uvicorn

from fauxcall_reference_api import API_TITLE, __version__
from fauxcall_reference_api.app import create_app
from fauxcall_reference_api.logs import configure_logging
from fauxcall_reference_api.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Sequence

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def build_parser() -> argparse.ArgumentParser:
    """The CLI. Unset options fall back to ``REFAPI_*`` environment variables, then defaults."""
    parser = argparse.ArgumentParser(
        prog="fauxcall-reference-api",
        description=f"{API_TITLE}: a deterministic ground-truth API for FauxCall.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--host", help="Interface to bind (default 127.0.0.1).")
    parser.add_argument("--port", type=int, help="Port to bind (default 9100).")
    parser.add_argument(
        "--no-latency",
        dest="latency",
        action="store_false",
        default=None,
        help="Disable the artificial 15-60 ms latency.",
    )
    parser.add_argument("--log-level", choices=_LOG_LEVELS, type=str.upper, help="Log level.")
    return parser


def load_settings(argv: Sequence[str] | None = None) -> Settings:
    """Environment/defaults overridden by any command-line options given."""
    args = build_parser().parse_args(argv)
    overrides = {
        name: value
        for name, value in vars(args).items()
        if name in Settings.model_fields and value is not None
    }
    base = Settings()
    return Settings.model_validate({**base.model_dump(), **overrides})


def main(argv: Sequence[str] | None = None) -> None:
    """Run the server with uvicorn (blocks until interrupted)."""
    settings = load_settings(argv)
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )
