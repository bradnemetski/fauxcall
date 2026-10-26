"""The everything server's stateless tools, as its source and the recordings show them."""

from __future__ import annotations

import base64
import copy
import math
from typing import TYPE_CHECKING, Final

from fauxcall_everything import jsfmt, zod
from fauxcall_everything.assets import MCP_TINY_IMAGE
from fauxcall_everything.common import (
    ToolError,
    js_num,
    locale_time,
    request_params,
    text,
    validated_tool,
)

if TYPE_CHECKING:
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

RESOURCE_TYPES: Final = ("Text", "Blob")
TEXT_URI: Final = "demo://resource/dynamic/text/"
BLOB_URI: Final = "demo://resource/dynamic/blob/"
_TIMER_MAX_MS: Final = 2_147_483_647
"""``setTimeout`` runs delays outside 1 ms .. 2**31-1 ms after 1 ms."""
WEATHER: Final[dict[str, JsonObject]] = {
    "New York": {"temperature": 33, "conditions": "Cloudy", "humidity": 82},
    "Chicago": {"temperature": 36, "conditions": "Light rain / drizzle", "humidity": 82},
    "Los Angeles": {"temperature": 73, "conditions": "Sunny / Clear", "humidity": 48},
}
_ANNOTATED: Final[dict[str, JsonObject]] = {
    "error": {
        "type": "text",
        "text": "Error: Operation failed",
        "annotations": {"audience": ["user", "assistant"], "priority": 1},
    },
    "success": {
        "type": "text",
        "text": "Operation completed successfully",
        "annotations": {"audience": ["user"], "priority": 0.7},
    },
    "debug": {
        "type": "text",
        "text": "Debug: Cache hit ratio 0.95, latency 150ms",
        "annotations": {"audience": ["assistant"], "priority": 0.3},
    },
}


def text_resource(uri: str, resource_id: float, now_ns: int) -> JsonObject:
    """``textResource(uri, id)``: a plaintext resource stamped with the time it was made."""
    body = (
        f"Resource {jsfmt.number_text(resource_id)}: This is a plaintext resource created at "
        f"{locale_time(now_ns)}"
    )
    return {"uri": uri, "mimeType": "text/plain", "text": body}


def blob_resource(uri: str, resource_id: float, now_ns: int) -> JsonObject:
    """``blobResource(uri, id)``: the same, base64-encoded."""
    body = (
        f"Resource {jsfmt.number_text(resource_id)}: This is a base64 blob created at "
        f"{locale_time(now_ns)}"
    )
    return {
        "uri": uri,
        "mimeType": "text/plain",
        "blob": base64.b64encode(body.encode("utf-8")).decode("ascii"),
    }


def dynamic_resource(kind: str, resource_id: float, now_ns: int) -> JsonObject:
    """A template resource by type (``Text``/``Blob``) and id, with its canonical URI."""
    if kind == "Text":
        return text_resource(TEXT_URI + jsfmt.number_text(resource_id), resource_id, now_ns)
    return blob_resource(BLOB_URI + jsfmt.number_text(resource_id), resource_id, now_ns)


def recorded_text(ctx: RequestContext, tool: str) -> JsonValue:
    """The result a tool that always answers the same answered when recorded."""
    for exchange in ctx.runtime.recordings.for_endpoint(f"MCP tools/call:{tool}"):
        reply = exchange.response.body.parsed() if exchange.response else None
        result = reply.get("result") if isinstance(reply, dict) else None
        if isinstance(result, dict) and not result.get("isError"):
            return copy.deepcopy(result)
    raise ToolError(f"no recorded answer for {tool}")


@validated_tool("echo", [zod.Field("message", "string")])
async def echo(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """``Echo: <message>``."""
    del ctx
    return {"content": [text(f"Echo: {args['message']}")]}


@validated_tool(
    "get-annotated-message",
    [
        zod.Field("messageType", "enum", values=("error", "success", "debug")),
        zod.Field("includeImage", "boolean", default=False),
    ],
)
async def get_annotated_message(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """A message with priority/audience annotations, optionally an annotated image."""
    del ctx
    content: list[JsonValue] = [copy.deepcopy(_ANNOTATED[str(args["messageType"])])]
    if args["includeImage"]:
        content.append(
            {
                "type": "image",
                "data": MCP_TINY_IMAGE,
                "mimeType": "image/png",
                "annotations": {"audience": ["user"], "priority": 0.5},
            }
        )
    return {"content": content}


@validated_tool("get-env", [])
async def get_env(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """The server process's environment, as recorded (it never changes while the server runs)."""
    del args
    return recorded_text(ctx, "get-env")


@validated_tool("get-tiny-image", [])
async def get_tiny_image(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """The MCP logo between two texts."""
    del ctx, args
    return {
        "content": [
            text("Here's the image you requested:"),
            {"type": "image", "data": MCP_TINY_IMAGE, "mimeType": "image/png"},
            text("The image above is the MCP logo."),
        ]
    }


@validated_tool(
    "get-resource-links",
    [zod.Field("count", "number", default=3, minimum=1, maximum=10)],
)
async def get_resource_links(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """``count`` links to the dynamic resources: blobs at odd ids, texts at even ones."""
    del ctx
    count = jsfmt.to_float(args["count"])
    content: list[JsonValue] = [
        text(
            f"Here are {jsfmt.number_text(count)} resource links to resources available in this "
            "server:"
        )
    ]
    resource_id = 1
    while resource_id <= count:
        even = resource_id % 2 == 0  # the source calls it isOdd
        content.append(
            {
                "name": f"{'Text' if even else 'Blob'} Resource {resource_id}",
                "uri": f"{TEXT_URI if even else BLOB_URI}{resource_id}",
                "description": f"Resource {resource_id}: plaintext resource",
                "mimeType": "text/plain",
                "type": "resource_link",
            }
        )
        resource_id += 1
    return {"content": content}


@validated_tool(
    "get-resource-reference",
    [
        zod.Field("resourceType", "enum", values=RESOURCE_TYPES, default="Text"),
        zod.Field("resourceId", "number", default=1),
    ],
)
async def get_resource_reference(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """An embedded dynamic resource, made now, between two texts."""
    resource_id = jsfmt.to_float(args["resourceId"])
    if not jsfmt.is_positive_integer(resource_id):
        raise ToolError(
            f"Invalid resourceId: {jsfmt.js_string(args['resourceId'])}. "
            "Must be a finite positive integer."
        )
    resource = dynamic_resource(str(args["resourceType"]), resource_id, ctx.now_ns)
    label = jsfmt.number_text(resource_id)
    return {
        "content": [
            text(f"Returning resource reference for Resource {label}:"),
            {"type": "resource", "resource": resource},
            text(f"You can access this resource using the URI: {resource['uri']}"),
        ]
    }


@validated_tool(
    "get-structured-content",
    [zod.Field("location", "enum", values=tuple(WEATHER))],
)
async def get_structured_content(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """Canned weather as structured content and as its JSON text."""
    del ctx
    weather = copy.deepcopy(WEATHER[str(args["location"])])
    return {"content": [text(jsfmt.stringify(weather, 0))], "structuredContent": weather}


@validated_tool("get-sum", [zod.Field("a", "number"), zod.Field("b", "number")])
async def get_sum(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """``The sum of a and b is a+b.``, numbers written as JavaScript writes them."""
    del ctx
    a, b = jsfmt.to_float(args["a"]), jsfmt.to_float(args["b"])
    total = a + b
    return {
        "content": [
            text(
                f"The sum of {jsfmt.number_text(a)} and {jsfmt.number_text(b)} is "
                f"{jsfmt.number_text(total)}."
            )
        ]
    }


def _timer_seconds(seconds: float) -> float:
    """How long ``setTimeout(resolve, seconds * 1000)`` really waits."""
    milliseconds = seconds * 1000
    if math.isnan(milliseconds) or not 1 <= milliseconds <= _TIMER_MAX_MS:
        milliseconds = 1
    return milliseconds / 1000


@validated_tool(
    "trigger-long-running-operation",
    [zod.Field("duration", "number", default=10), zod.Field("steps", "number", default=5)],
)
async def trigger_long_running_operation(ctx: RequestContext, args: JsonObject) -> JsonValue:
    """``steps`` waits of ``duration/steps`` seconds, a progress notification after each."""
    duration, steps = jsfmt.to_float(args["duration"]), jsfmt.to_float(args["steps"])
    meta = request_params(ctx).get("_meta")
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    step_seconds = _timer_seconds(duration / steps if steps else math.copysign(math.inf, duration))
    if steps == 0 and duration == 0:
        step_seconds = _timer_seconds(math.nan)
    step = 1
    while step < steps + 1:
        await ctx.pause(step_seconds)
        if token is not None:
            ctx.notify(
                "notifications/progress",
                {"progress": step, "total": js_num(args["steps"]), "progressToken": token},
            )
        step += 1
    return {
        "content": [
            text(
                f"Long running operation completed. Duration: {jsfmt.number_text(duration)} "
                f"seconds, Steps: {jsfmt.number_text(steps)}."
            )
        ]
    }
