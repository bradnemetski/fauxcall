"""Stdlib logging: one plain-text format for the app, uvicorn and the MCP SDK."""

from __future__ import annotations

import logging
from typing import Final

LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOGGER_NAME: Final = "fauxcall_reference_api"


def configure_logging(level: str) -> None:
    """Install a single stderr handler on the root logger (replacing any existing ones)."""
    logging.basicConfig(level=level, format=LOG_FORMAT, force=True)
    logging.getLogger(LOGGER_NAME).setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the package namespace."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
