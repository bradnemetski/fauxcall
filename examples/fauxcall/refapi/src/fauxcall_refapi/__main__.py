"""Run the fake with uvicorn.

Start it with ``uv run fauxcall-refapi``
(or ``python -m fauxcall_refapi``).
"""

from __future__ import annotations

import uvicorn

from fauxcall_refapi.app import build_app
from fauxcall_refapi.settings import Settings


def main() -> None:
    """Serve the fake (logging is configured by FauxCall)."""
    settings = Settings()
    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
