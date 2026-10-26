"""Custom behavior of this fake: yours to edit, ``fauxcall scaffold`` never overwrites it.

Most endpoints are served by FauxCall's generic engine straight from ``data/model.json``. Map an
endpoint id here to replace the generic behavior with code (role ``computed``/``custom``)::

    async def latest_rates(ctx: RequestContext) -> Response:
        base = str(ctx.params.get("base") or "EUR")
        rates = ...  # derive from ctx.world (the consistent dataset), ctx.now_ns (the fake clock)
        return ctx.respond({"base": base, "rates": rates})

    HANDLERS["GET /v1/latest"] = latest_rates

MCP tools whose behavior is ``computed`` work the same way through ``TOOL_HANDLERS`` (tool name ->
``async (ctx, arguments) -> result``), and any other MCP method through ``METHOD_HANDLERS``
(``"resources/read"``, ``"prompts/get"``, ``"completion/complete"`` ... -> ``async (ctx, params) ->
result``; return ``None`` to let the recordings answer). A client's notifications
(``"notifications/roots/list_changed"`` ...) reach their handler there too, in the background.
Raise ``RpcError`` for a JSON-RPC error, ``EarlyResponse`` for an HTTP-level reply;
``ctx.state`` keeps per-session state, ``ctx.notify`` sends progress and log notifications.

Useful helpers on the context: ``ctx.world`` (records), ``ctx.params`` (validated parameters),
``ctx.json_body``, ``ctx.now_ns`` / ``ctx.calendar(name)`` (time), ``ctx.respond`` / ``ctx.error``
(real-API-shaped responses), ``ctx.render_record`` (applies field policies). Handlers that write
keep references as the model's relations say: ``ctx.refuse_missing(collection, record)`` before
storing, and ``ctx.delete(collection, key)`` instead of ``ctx.world.delete``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fauxcall_everything import client_tools, jsfmt, prompts, resources, tasks, tools, zod
from fauxcall_everything.common import check_cursor, protocol_error

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Response, WebSocket
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

METHOD_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP method -> custom handler of its params (``None``: the recordings answer)."""


async def tool_call_shape(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tools/call`` refused by the SDK's schema: a name that isn't text, arguments not an object.

    Anything else is left to FauxCall (``None``), which finds the tool and calls its handler.
    """
    tasks.check_related_task(ctx, params)  # the SDK looks the task up before anything else
    name = params.get("name", jsfmt.UNDEFINED)
    if not isinstance(name, str):
        raise protocol_error([zod.invalid_type("string", name, ["params", "name"])])
    arguments = params.get("arguments", jsfmt.UNDEFINED)
    if not isinstance(arguments, dict | jsfmt.Undefined):
        raise protocol_error([zod.invalid_type("record", arguments, ["params", "arguments"])])
    return None


async def listing(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``tools/list``, ``prompts/list``, ``resources/templates/list``: the recorded listings.

    Only the checks the SDK makes first are here (a cursor that isn't text, an unknown related
    task); ``None`` leaves the listing to FauxCall.
    """
    tasks.check_related_task(ctx, params)
    check_cursor(params)
    return None


async def ping(ctx: RequestContext, params: JsonObject) -> JsonValue:
    """``ping``: an empty result, unless it names an unknown related task."""
    tasks.check_related_task(ctx, params)
    return {}


def related_task_checked(
    handler: Callable[[RequestContext, JsonObject], Awaitable[JsonValue]],
) -> Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]:
    """The handler, after the SDK's lookup of the request's related task (``_meta``)."""

    async def checked(ctx: RequestContext, params: JsonObject) -> JsonValue:
        tasks.check_related_task(ctx, params)
        return await handler(ctx, params)

    checked.__name__ = handler.__name__
    checked.__doc__ = handler.__doc__
    return checked


TOOL_HANDLERS.update(
    {
        "echo": tools.echo,
        "get-annotated-message": tools.get_annotated_message,
        "get-env": tools.get_env,
        "get-resource-links": tools.get_resource_links,
        "get-resource-reference": tools.get_resource_reference,
        "get-structured-content": tools.get_structured_content,
        "get-sum": tools.get_sum,
        "get-tiny-image": tools.get_tiny_image,
        "gzip-file-as-resource": resources.gzip_file_as_resource,
        "toggle-simulated-logging": resources.toggle_simulated_logging,
        "toggle-subscriber-updates": resources.toggle_subscriber_updates,
        "trigger-long-running-operation": tools.trigger_long_running_operation,
        "get-roots-list": client_tools.get_roots_list,
        "trigger-elicitation-request": client_tools.trigger_elicitation_request,
        "trigger-url-elicitation": client_tools.trigger_url_elicitation,
        "trigger-sampling-request": client_tools.trigger_sampling_request,
        "simulate-research-query": tasks.research_query,
        "trigger-sampling-request-async": client_tools.trigger_sampling_request_async,
        "trigger-elicitation-request-async": client_tools.trigger_elicitation_request_async,
    }
)

METHOD_HANDLERS.update(
    {
        "tools/call": tool_call_shape,
        "tools/list": listing,
        "prompts/list": listing,
        "resources/templates/list": listing,
        "ping": ping,
        **{
            method: related_task_checked(handler)
            for method, handler in {
                "resources/read": resources.read_resource,
                "resources/list": resources.list_resources,
                "resources/subscribe": resources.subscribe,
                "resources/unsubscribe": resources.unsubscribe,
                "logging/setLevel": resources.set_level,
                "prompts/get": prompts.get_prompt,
                "completion/complete": prompts.complete,
                "tasks/get": tasks.get_task,
                "tasks/result": tasks.task_result,
                "tasks/list": tasks.list_tasks,
                "tasks/cancel": tasks.cancel_task,
            }.items()
        },
        "notifications/initialized": client_tools.initialized,
        "notifications/roots/list_changed": client_tools.roots_changed,
    }
)
