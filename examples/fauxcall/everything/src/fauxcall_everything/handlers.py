"""Custom behavior of this fake: yours to edit, ``fauxcall scaffold`` never overwrites it.

Every tool of the Everything server is ``computed`` (``tools.py``), and its other methods —
resources, prompts, completions, logging, subscriptions, tasks, the roots handshake — are answered
by ``methods.py``: the real server fabricates most of its answers (times, ids, per-session state),
so replaying recordings would not do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fauxcall_everything.methods import METHOD_HANDLERS as _METHODS
from fauxcall_everything.tools import TOOL_HANDLERS as _TOOLS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Response, WebSocket
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = dict(
    _TOOLS
)
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

METHOD_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = dict(
    _METHODS
)
"""MCP method -> custom handler of its params (``None``: the recordings answer)."""
