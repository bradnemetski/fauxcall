"""What the real server keeps between calls, per connected client and across them.

The real server builds one ``McpServer`` per MCP session (its task store, session resources,
logging level, roots cache), and keeps a few module-level maps shared by every session: who is
subscribed to which URI (in the order URIs were first subscribed), and which session registered
each session-resource URI (a second session registering the same name removes the first one's).
Here all of it lives in ``ctx.state`` (per FauxCall evaluation session, cleared by reset).
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import asyncio

    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.runtime.app import FakeRuntime
    from fauxcall.types import JsonObject, JsonValue

    from fauxcall_everything.tasks import TaskStore

STATE_KEY: Final = "everything"
LOG_SEVERITY: Final = {
    "debug": 0,
    "info": 1,
    "notice": 2,
    "warning": 3,
    "error": 4,
    "critical": 5,
    "alert": 6,
    "emergency": 7,
}
"""The SDK's ``LOG_LEVEL_SEVERITY``: messages below a session's chosen level aren't sent."""


@dataclass(slots=True)
class SessionResource:
    """A resource ``gzip-file-as-resource`` registered for its session."""

    uri: str
    name: str
    mime_type: str
    blob: str

    def listing(self) -> JsonObject:
        """How ``resources/list`` shows it."""
        return {"uri": self.uri, "name": self.name, "mimeType": self.mime_type}


@dataclass(slots=True)
class McpSession:
    """One connected client's server."""

    id: str
    log_level: str | None = None
    roots: list[JsonValue] | None = None
    """The client's roots once it answered ``roots/list`` (``None``: never synced)."""
    resources: dict[str, SessionResource] = field(default_factory=dict)
    """Registered session resources by URI, in registration order."""
    logging: asyncio.Task[None] | None = None
    updates: asyncio.Task[None] | None = None
    tasks: TaskStore | None = None
    error_path_issued: set[str] = field(default_factory=set)

    def logs_at(self, level: str) -> bool:
        """Whether a message at ``level`` reaches this client (``isMessageIgnored``)."""
        if self.log_level is None:
            return True
        return LOG_SEVERITY.get(level, 0) >= LOG_SEVERITY.get(self.log_level, 0)


@dataclass(slots=True)
class Server:
    """Everything the real server process holds, for one FauxCall evaluation session."""

    runtime: FakeRuntime
    fauxcall_session: str
    sessions: dict[str, McpSession] = field(default_factory=dict)
    subscriptions: dict[str, list[str]] = field(default_factory=dict)
    """URI -> the MCP sessions subscribed to it (URIs in first-subscribed order)."""
    resource_owners: dict[str, str] = field(default_factory=dict)
    """Session-resource URI -> the MCP session that registered it last."""

    def session(self, mcp_session: str | None) -> McpSession:
        """The state of one client's server (created on first use)."""
        key = mcp_session or ""
        found = self.sessions.get(key)
        if found is None:
            found = self.sessions[key] = McpSession(key)
        return found

    def alive(self) -> bool:
        """Whether this is still the evaluation session's state (no reset since)."""
        return self.runtime.handler_state(self.fauxcall_session).get(STATE_KEY) is self

    def channel(self, mcp_session: str) -> Channel:
        """The GET-stream channel of any connected client (to notify another session)."""
        from fauxcall.runtime import Channel  # noqa: PLC0415 - runtime import only

        fake = self.runtime.mcp
        assert fake is not None  # noqa: S101 - this fake is an MCP server
        return Channel(fake, mcp_session)


@dataclass(slots=True)
class Transport:
    """What the connection itself established: kept across resets, like the runtime's sessions.

    A reset puts the server's state back, but a client that stays connected keeps its session,
    the tools registered at its ``notifications/initialized`` and its ``roots/list_changed``
    handler, as the runtime keeps the session and its offered tools.
    """

    initialized: set[str] = field(default_factory=set)
    """MCP sessions whose client sent ``notifications/initialized``."""
    roots_synced: set[str] = field(default_factory=set)
    """MCP sessions where ``syncRoots`` installed its ``roots/list_changed`` handler."""


_TRANSPORTS: Final[weakref.WeakKeyDictionary[FakeRuntime, Transport]] = weakref.WeakKeyDictionary()


def transport_of(ctx: RequestContext) -> Transport:
    """The connection facts of this fake."""
    return _TRANSPORTS.setdefault(ctx.runtime, Transport())


def server_of(ctx: RequestContext) -> Server:
    """The server state for this request's evaluation session."""
    found = ctx.state.get(STATE_KEY)
    if not isinstance(found, Server):
        found = Server(ctx.runtime, ctx.session)
        ctx.state[STATE_KEY] = found
    return found


def session_of(ctx: RequestContext) -> McpSession:
    """The state of the calling client's server."""
    return server_of(ctx).session(ctx.mcp_session)


def log_message(
    channel: Channel, session: McpSession, level: str, data: str, logger: str | None = None
) -> None:
    """``server.sendLoggingMessage``: a ``notifications/message`` on the client's GET stream."""
    if not session.logs_at(level):
        return
    params: JsonObject = {"level": level}
    if logger is not None:
        params["logger"] = logger
    params["data"] = data
    channel.push("notifications/message", params)


def stop(task: asyncio.Task[None] | None) -> None:
    """Stop background work (a simulated logging or updates interval)."""
    if task is not None and not task.done():
        task.cancel()
