"""FauxCall reference API: a deterministic market-data API used as ground truth for FauxCall.

It is a *real* API (not a fake): FauxCall records it like any third-party service, generates a
fake from the recordings, and runs parity tests real-vs-fake. Every behavior is specified in
``GROUND_TRUTH.md`` next to this package's ``pyproject.toml``.
"""

from __future__ import annotations

from typing import Final

__all__ = ["API_TITLE", "API_VERSION", "__version__"]

__version__: Final = "1.0.0"
API_VERSION: Final = "1.0.0"
API_TITLE: Final = "FauxCall Reference Market API"
