"""Custom behavior of this fake: yours to edit, ``fauxcall scaffold`` never overwrites it.

The Everything server is a demo of every MCP feature, so most of it is code rather than
recordings: every tool but ``get-env`` (the server's environment, replayed) is computed in
:mod:`fauxcall_everything.tools`, tasks in :mod:`~fauxcall_everything.tasks`, resources,
prompts, completions, subscriptions and logging levels in :mod:`~fauxcall_everything.resources`.
What the real server keeps between calls lives in ``ctx.state`` (:mod:`~fauxcall_everything.state`),
per client (``Mcp-Session-Id``) as the real server keeps one ``McpServer`` per session.

Every request first goes through the transport's check of its params
(:mod:`~fauxcall_everything.requests`). Two methods are handled here:

* ``tools/call`` first, for what the SDK checks before any tool runs: the request's schema, and
  a ``task`` member asking for a task (only ``simulate-research-query`` creates one; any other
  tool runs and is then refused with ``Invalid task creation result``).
* ``notifications/initialized``: the real server registers the capability-dependent tools then
  (one ``notifications/tools/list_changed`` each) and asks a roots-capable client for its roots
  350 ms later.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fauxcall.runtime import RpcError

from fauxcall_everything import requests, resources, tasks, zod
from fauxcall_everything.state import session_of, transport_of
from fauxcall_everything.tools import TOOLS, request_roots, sync_roots

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Response, WebSocket
    from fauxcall.runtime import Channel, RequestContext
    from fauxcall.types import JsonObject, JsonValue

    from fauxcall_everything.requests import Handler

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = dict(TOOLS)
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

ROOTS_SYNC_DELAY_S: Final = 0.35
"""``setTimeout(() => syncRoots(...), 350)`` after the client's ``notifications/initialized``."""
RESEARCH_TOOL: Final = "simulate-research-query"


def _offered(ctx: RequestContext, name: str) -> bool:
    """Whether this client's server has registered the tool (capabilities, initialized)."""
    mcp = ctx.model.mcp
    tool = mcp.tool(name) if mcp is not None else None
    if tool is None or not tool.offered_to(ctx.client_capabilities):
        return False
    waits = bool(tool.requires) and mcp is not None and mcp.capability_tools_after_initialized
    return not waits or (ctx.mcp_session or "") in transport_of(ctx).initialized


def _call_issues(ctx: RequestContext, params: JsonObject) -> list[zod.Issue]:
    """``CallToolRequestSchema``'s issues, in its member order: task, name, arguments."""
    missing = requests.params_issue(ctx)
    if missing is not None:
        return [missing]
    issues: list[zod.Issue] = []
    if "task" in params:
        task = params["task"]
        if not isinstance(task, dict):
            issues.append(zod.wrong_type("object", task, ["params", "task"]))
        elif "ttl" in task:
            _, problems = zod.number()(task["ttl"], ["params", "task", "ttl"])
            issues += problems
    if not isinstance(params.get("name"), str):
        issues.append(zod.wrong_type("string", params.get("name", zod.MISSING), ["params", "name"]))
    if "arguments" in params and not isinstance(params["arguments"], dict):
        issues.append(zod.wrong_type("record", params["arguments"], ["params", "arguments"]))
    return issues


async def call_tool(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tools/call`` before FauxCall's own checks: the request, then task augmentation.

    ``None`` lets FauxCall call the tool (``simulate-research-query`` refuses calls without a
    task in its handler).
    """
    requests.refuse(_call_issues(ctx, params))
    if "task" not in params:
        return None
    name = str(params["name"])
    arguments = params.get("arguments", {})
    assert isinstance(arguments, dict)  # noqa: S101 - checked above
    if not _offered(ctx, name):
        raise RpcError(-32602, tasks.invalid_task_result())
    if name == RESEARCH_TOOL:
        return tasks.start_research(ctx, arguments)
    handler = TOOL_HANDLERS.get(name)
    if handler is not None:
        await handler(ctx, arguments)  # it runs, side effects and all; its answer isn't a task
    raise RpcError(-32602, tasks.invalid_task_result())


async def initialized(ctx: RequestContext, _params: JsonObject) -> JsonValue:
    """``notifications/initialized``: conditional tools registered, roots synced 350 ms later.

    Only the first one counts: the real server fails to register its tools a second time, and
    sends nothing.
    """
    transport = transport_of(ctx)
    key = ctx.mcp_session or ""
    if key in transport.initialized:
        return None
    transport.initialized.add(key)
    mcp = ctx.model.mcp
    channel = ctx.channel
    assert channel is not None  # noqa: S101 - every MCP message has one
    for tool in mcp.tools if mcp is not None else []:
        if tool.requires and tool.offered_to(ctx.client_capabilities):
            channel.push("notifications/tools/list_changed")
    if "roots" in ctx.client_capabilities:
        await channel.pause(ROOTS_SYNC_DELAY_S)  # wall clock: it lets the GET stream open first
        await sync_roots(ctx, session_of(ctx), channel)
    return None


async def roots_changed(ctx: RequestContext, _params: JsonObject) -> JsonValue:
    """``notifications/roots/list_changed``: ask again, once roots were synced for this client."""
    channel: Channel | None = ctx.channel
    if (ctx.mcp_session or "") in transport_of(ctx).roots_synced and channel is not None:
        await request_roots(session_of(ctx), channel, ctx)
    return None


_REQUESTS: Final[dict[str, Handler]] = {
    "tools/call": call_tool,
    "resources/read": resources.read_resource,
    "resources/list": resources.list_resources,
    "resources/subscribe": resources.subscribe,
    "resources/unsubscribe": resources.unsubscribe,
    "logging/setLevel": resources.set_level,
    "prompts/get": resources.get_prompt,
    "completion/complete": resources.complete,
    "tasks/get": tasks.tasks_get,
    "tasks/list": tasks.tasks_list,
    "tasks/cancel": tasks.tasks_cancel,
    "tasks/result": tasks.tasks_result,
    "tools/list": requests.recorded,
    "prompts/list": requests.recorded,
    "resources/templates/list": requests.recorded,
    "ping": requests.recorded,
}

METHOD_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {
    **{method: requests.guarded(handler) for method, handler in _REQUESTS.items()},
    "notifications/initialized": initialized,
    "notifications/roots/list_changed": roots_changed,
}
"""MCP method -> custom handler of its params (``None``: the recordings answer)."""
