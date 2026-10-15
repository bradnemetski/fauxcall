"""What the real server keeps per connected client (MCP session).

The real server builds one ``McpServer`` per MCP session and keeps module-level maps keyed by the
session id: session resources (gzip-file-as-resource), the client's logging level, whether
simulated logging or resource updates run, the roots it reported, the URL-elicitation error-path
markers and its tasks. Two maps are shared by every client: session resources by URI, and
resource subscriptions (URI -> subscribed sessions, in the order URIs were first subscribed).
FauxCall's ``ctx.state`` is per *evaluation* session (``X-Fauxcall-Session``), so each MCP session
gets its own :class:`ClientState` inside it, and the shared maps are shared within it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from fauxcall.runtime import Channel, FakeRuntime, RequestContext
    from fauxcall.types import JsonObject

    from fauxcall_everything.tasks import ResearchTask

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
_CLIENTS: Final = "clients"
_OWNERS: Final = "session_resource_owners"
_SUBSCRIBERS: Final = "subscribers"


@dataclass(slots=True)
class SessionResource:
    """A resource gzip-file-as-resource registered for this client."""

    uri: str
    name: str
    mime_type: str
    blob: str


@dataclass(slots=True)
class ClientState:
    """Everything the real server remembers about one connected client."""

    resources: dict[str, SessionResource] = field(default_factory=dict)
    """Session resources by their (raw) URI, in registration order."""
    log_level: str | None = None
    logging: asyncio.Task[None] | None = None
    updates: asyncio.Task[None] | None = None
    roots: list[JsonObject] | None = None
    """The roots the client reported (``None``: never synced)."""
    roots_watch: bool = False
    """Whether roots/list_changed is handled yet (the real server adds the handler at its sync)."""
    url_markers: set[str] = field(default_factory=set)
    tasks: dict[str, ResearchTask] = field(default_factory=dict)

    def logs(self, level: str) -> bool:
        """Whether a log message of ``level`` reaches this client (its logging/setLevel)."""
        if self.log_level is None:
            return True
        return LOG_LEVELS.index(level) >= LOG_LEVELS.index(self.log_level)


def client_state(ctx: RequestContext) -> ClientState:
    """The state of the client (MCP session) this request comes from."""
    clients: dict[str, ClientState] = ctx.state.setdefault(_CLIENTS, {})
    return clients.setdefault(ctx.mcp_session or "", ClientState())


def is_current(ctx: RequestContext, state: ClientState) -> Callable[[], bool]:
    """Whether ``state`` is still this client's: a reset of the fake starts every client afresh.

    Work left running in the background (simulated logging, resource updates, task status
    pushes) checks it, so a reset also ends what a client had started, as a new session would.
    """
    runtime, session, client = ctx.runtime, ctx.session, ctx.mcp_session or ""

    def current() -> bool:
        clients = runtime.handler_state(session).get(_CLIENTS, {})
        return bool(clients.get(client) is state)

    return current


def tasks_of(runtime: FakeRuntime, session: str, client: str) -> dict[str, ResearchTask]:
    """A client's tasks, looked up outside a request (the front checks related tasks)."""
    clients: dict[str, ClientState] = runtime.handler_state(session).get(_CLIENTS, {})
    state = clients.get(client)
    return state.tasks if state is not None else {}


def resource_owners(ctx: RequestContext) -> dict[str, tuple[ClientState, Channel | None]]:
    """Session resource URI -> the client that registered it last, and its channel.

    The real server tracks session resources in one module-level map for every session, so a
    second client registering the same name takes it away from the first one.
    """
    owners: dict[str, tuple[ClientState, Channel | None]] = ctx.state.setdefault(_OWNERS, {})
    return owners


def subscribers(ctx: RequestContext) -> dict[str, set[str]]:
    """URI -> the MCP sessions subscribed to it, for every client (``resources/subscriptions.js``).

    One map for all sessions, never shrinking: a URI keeps its place once anyone subscribed, so a
    client's updates follow the order URIs were first subscribed by any client.
    """
    found: dict[str, set[str]] = ctx.state.setdefault(_SUBSCRIBERS, {})
    return found


def log(
    state: ClientState, channel: Channel | None, level: str, data: str, *, logger: str | None = None
) -> None:
    """``server.sendLoggingMessage``: a notifications/message on the client's GET stream."""
    if channel is None or not state.logs(level):
        return
    params: JsonObject = {"level": level}
    if logger is not None:
        params["logger"] = logger
    params["data"] = data
    channel.push("notifications/message", params)
