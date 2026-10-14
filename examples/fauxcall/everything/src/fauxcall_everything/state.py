"""What the real server keeps per connected client (MCP session), and the assets it serves."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import asyncio

    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonValue

SERVER_ZONE: Final = ZoneInfo("America/New_York")
"""The real server's local zone (its toLocaleTimeString() read 9:40 PM at 01:40Z in October)."""
LOG_LEVELS: Final = (
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
)


@dataclass(slots=True)
class SessionResource:
    """A file ``gzip-file-as-resource`` registered for one session."""

    uri: str
    name: str
    mime_type: str
    blob: str


@dataclass(slots=True)
class ResearchTask:
    """A ``simulate-research-query`` task: its timeline on the fake clock."""

    task_id: str
    topic: str
    ambiguous: bool
    created_ns: int
    answered_ns: int | None = None
    """When the clarification arrived (ambiguous tasks only)."""
    clarification: str | None = None
    cancelled_ns: int | None = None
    completed_ns: int | None = None
    """Set once the result was handed out before the clock reached it (fast mode, frozen clock)."""
    result_taken: bool = False


@dataclass(slots=True)
class McpSession:
    """One connected client's state, as the real server keeps it."""

    resources: dict[str, SessionResource] = field(default_factory=dict)
    logging_task: asyncio.Task[None] | None = None
    updates_task: asyncio.Task[None] | None = None
    log_level: str | None = None
    roots: list[Any] | None = None
    roots_watch: bool = False
    tasks: dict[str, ResearchTask] = field(default_factory=dict)
    url_errors: set[str] = field(default_factory=set)


def session_id(ctx: RequestContext) -> str:
    """The client's ``Mcp-Session-Id`` (what the real server prints in its texts)."""
    return ctx.mcp_session or ""


def mcp_session(ctx: RequestContext) -> McpSession:
    """The state of the client making this request."""
    sessions: dict[str, McpSession] = ctx.state.setdefault("mcp_sessions", {})
    return sessions.setdefault(session_id(ctx), McpSession())


def subscriptions(ctx: RequestContext) -> dict[str, set[str]]:
    """Resource URI -> subscribed sessions, in the order URIs were first subscribed."""
    found: dict[str, set[str]] = ctx.state.setdefault("subscriptions", {})
    return found


def resource_owners(ctx: RequestContext) -> dict[str, str]:
    """Session resource URI -> the session that registered it last (the server's global map)."""
    found: dict[str, str] = ctx.state.setdefault("resource_owners", {})
    return found


def local_time(now_ns: int) -> str:
    """``new Date().toLocaleTimeString()`` on the real server (en-US, its zone)."""
    moment = datetime.fromtimestamp(now_ns / 1e9, UTC).astimezone(SERVER_ZONE)
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment:%M:%S} {'AM' if moment.hour < 12 else 'PM'}"  # noqa: PLR2004


def iso_ms(ns: int) -> str:
    """``new Date(ms).toISOString()``."""
    seconds, millis = divmod(ns // 1_000_000, 1000)
    moment = datetime.fromtimestamp(seconds, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def log_allowed(session: McpSession, level: str) -> bool:
    """The SDK sends a log message only at or above the level the client set."""
    if session.log_level is None:
        return True
    return LOG_LEVELS.index(level) >= LOG_LEVELS.index(session.log_level)


@cache
def assets() -> dict[str, JsonValue]:
    """Static content recorded from the real server (documents, image, environment, README)."""
    text = resources.files("fauxcall_everything").joinpath("assets/recorded.json").read_text()
    loaded: dict[str, JsonValue] = json.loads(text)
    return loaded
