"""The logging framework shared by the FauxCall CLI and every generated fake.

One pipeline for everything:

* **structlog** for our own code (``get_logger(__name__).info("event", key=value)``),
* the **stdlib bridge** so uvicorn, httpx2, the Jev SDK and any other library log through the same
  processors and renderer,
* **JSON lines** when output is not a terminal (containers, CI, log shippers) and a readable
  **console** format when it is,
* **context variables** (request id, session id, fake name …) bound once and attached to every line,
* **redaction** of anything that looks like a credential, however deeply nested.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import TYPE_CHECKING, Any, Final, Literal, TextIO

import structlog

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from structlog.typing import EventDict, Processor, WrappedLogger

LogFormat = Literal["auto", "json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

REDACTED: Final = "[REDACTED]"
_SECRET_KEY: Final = re.compile(
    r"(authorization|api[-_]?key|apikey|token|secret|password|passwd|cookie|credential|"
    r"private[-_]?key|client[-_]?secret|x-api-key|session[-_]?id)",
    re.IGNORECASE,
)
_NOISY_LIBRARIES: Final = ("httpx2", "httpcore2", "httpx", "httpcore", "hpack", "watchfiles")
_UVICORN_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access")


def is_secret_key(key: str) -> bool:
    """Whether a mapping key (header name, field name) is likely to hold a credential."""
    return bool(_SECRET_KEY.search(key))


def _redact(value: Any, depth: int = 0) -> Any:  # noqa: ANN401 - log values are arbitrary
    if depth > 8:  # noqa: PLR2004 - guard against pathological nesting
        return value
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and is_secret_key(key)
            else _redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(item, depth + 1) for item in value)
    return value


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Structlog processor that blanks out credential-looking values anywhere in the event."""
    for key in list(event_dict):
        if key == "event":
            continue
        if is_secret_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


def _shared_processors(static: dict[str, str]) -> list[Processor]:
    def add_static(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        for key, value in static.items():
            event_dict.setdefault(key, value)
        return event_dict

    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        add_static,
        redact_secrets,
    ]


def _resolve_format(fmt: LogFormat, stream: TextIO) -> Literal["json", "console"]:
    if fmt != "auto":
        return fmt
    isatty = getattr(stream, "isatty", None)
    return "console" if callable(isatty) and isatty() else "json"


def configure_logging(
    level: LogLevel | int = "INFO",
    fmt: LogFormat = "auto",
    *,
    stream: TextIO | None = None,
    static: MutableMapping[str, str] | None = None,
) -> None:
    """Configure structlog and the stdlib root logger once, for the whole process.

    Args:
        level: Minimum level for FauxCall and library logs.
        fmt: ``"json"``, ``"console"`` or ``"auto"`` (console on a TTY, JSON otherwise).
        stream: Where to write; defaults to ``sys.stderr``.
        static: Fields added to every line (e.g. ``{"fake": "petstore"}``).
    """
    output = stream if stream is not None else sys.stderr
    shared = _shared_processors(dict(static or {}))
    renderer: Processor
    if _resolve_format(fmt, output) == "json":
        renderer = structlog.processors.JSONRenderer()
        final: list[Processor] = [structlog.processors.format_exc_info, renderer]
    else:
        colors = bool(getattr(output, "isatty", lambda: False)())
        renderer = structlog.dev.ConsoleRenderer(colors=colors)
        final = [renderer]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *final],
    )
    handler = logging.StreamHandler(output)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    library_level = max(logging.WARNING, root.level)
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(library_level)
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structured logger. Use ``get_logger(__name__)`` in modules."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def bind_context(**values: object) -> None:
    """Attach key/values to every log line emitted from the current context (task/request)."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    """Remove every value bound with :func:`bind_context` in the current context."""
    structlog.contextvars.clear_contextvars()
