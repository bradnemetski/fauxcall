"""What the real server keeps per MCP session, and the pieces several handlers share.

The reference server builds one ``McpServer`` per session, so its state is per
``Mcp-Session-Id``: session resources, subscriptions, the logging level, roots, tasks, toggles.
FauxCall's ``ctx.state`` is per *evaluation* session, so it is keyed by the MCP session here.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from fauxcall_everything import jsvalues, zodlike

if TYPE_CHECKING:
    from fauxcall.runtime import RequestContext
    from fauxcall.runtime.mcp_server import Channel
    from fauxcall.types import JsonObject, JsonValue

SERVER_ZONE: Final = ZoneInfo("America/New_York")
"""The real server's local time zone (it prints ``toLocaleTimeString()``; recorded 23:54 UTC as
``7:54:20 PM``, on a host set to America/New_York)."""
LEVELS: Final = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")
_NOON: Final = 12


def of(ctx: RequestContext) -> dict[str, Any]:
    """This MCP session's state (FauxCall keeps it per evaluation session; reset clears it)."""
    every: dict[str, dict[str, Any]] = ctx.state.setdefault("mcp_sessions", {})
    return every.setdefault(ctx.mcp_session or "", {})


def local_time(now_ns: int) -> str:
    """``new Date().toLocaleTimeString()`` in en-US on the server: ``7:54:20 PM``."""
    moment = datetime.fromtimestamp(now_ns / 1e9, SERVER_ZONE)
    hour = moment.hour % _NOON or _NOON
    half = "AM" if moment.hour < _NOON else "PM"
    return f"{hour}:{moment.minute:02d}:{moment.second:02d} {half}"


def log(
    channel: Channel | None, state: dict[str, Any], level: str, data: str, logger: str | None = None
) -> None:
    """``server.sendLoggingMessage``: on the GET stream, unless below the session's level."""
    current = state.get("log_level")
    if channel is None or (current is not None and LEVELS.index(level) < LEVELS.index(current)):
        return
    params: JsonObject = {"level": level}
    if logger is not None:
        params["logger"] = logger
    params["data"] = data
    channel.push("notifications/message", params)


def text_uri(resource_id: float) -> str:
    """``textResourceUri(id)``."""
    return f"demo://resource/dynamic/text/{jsvalues.number_text(resource_id)}"


def blob_uri(resource_id: float) -> str:
    """``blobResourceUri(id)``."""
    return f"demo://resource/dynamic/blob/{jsvalues.number_text(resource_id)}"


def text_resource(uri: str, resource_id: float, now_ns: int) -> JsonObject:
    """A dynamic text resource: its creation time is the server's local time of day."""
    created = local_time(now_ns)
    return {
        "uri": uri,
        "mimeType": "text/plain",
        "text": f"Resource {jsvalues.number_text(resource_id)}: "
        f"This is a plaintext resource created at {created}",
    }


def blob_resource(uri: str, resource_id: float, now_ns: int) -> JsonObject:
    """A dynamic blob resource (served as ``text/plain``, as the real server does)."""
    text = (
        f"Resource {jsvalues.number_text(resource_id)}: "
        f"This is a base64 blob created at {local_time(now_ns)}"
    )
    return {
        "uri": uri,
        "mimeType": "text/plain",
        "blob": base64.b64encode(text.encode()).decode(),
    }


def _invalid_value(path: list[JsonValue], values: tuple[str, ...]) -> JsonObject:
    choices = "|".join(f'"{value}"' for value in values)
    listed: list[JsonValue] = [*values]
    return {
        "code": "invalid_value",
        "values": listed,
        "path": path,
        "message": f"Invalid option: expected one of {choices}",
    }


def _loose(value: JsonObject, known: tuple[str, ...], parsed: JsonObject) -> JsonObject:
    """A loose zod object's output: known keys in schema order, then the rest as sent."""
    ordered: JsonObject = {key: parsed[key] for key in known if key in parsed}
    ordered.update({key: item for key, item in value.items() if key not in known})
    return ordered


def _content_issues(content: JsonObject) -> list[JsonObject]:
    """Each value must be a string, number, boolean or list of strings (zod union, 000529)."""
    issues: list[JsonObject] = []
    for key, item in content.items():
        if isinstance(item, str | int | float | bool):
            continue
        strings_only = (
            [
                zodlike.type_issue([index], "string", element, present=True)
                for index, element in enumerate(item)
                if not isinstance(element, str)
            ]
            if isinstance(item, list)
            else []
        )
        if isinstance(item, list) and not strings_only:
            continue
        branches: list[JsonValue] = [
            [zodlike.type_issue([], kind, item, present=True)]
            for kind in ("string", "number", "boolean")
        ]
        array: list[JsonValue] = (
            [*strings_only]
            if isinstance(item, list)
            else [zodlike.type_issue([], "array", item, present=True)]
        )
        branches.append(array)
        where: list[JsonValue] = ["content", key]
        issues.append(
            {"code": "invalid_union", "errors": branches, "path": where, "message": "Invalid input"}
        )
    return issues


def elicit_result(value: JsonValue) -> tuple[JsonObject, list[JsonObject]]:
    """``ElicitResultSchema.parse``: the parsed result and zod's issues."""
    if not isinstance(value, dict):
        issue = zodlike.type_issue([], "object", value, present=value is not None)
        return {}, [issue]
    parsed: JsonObject = {}
    issues: list[JsonObject] = []
    if "_meta" in value:
        parsed["_meta"] = value["_meta"]
    action = value.get("action")
    if action in ("accept", "decline", "cancel"):
        parsed["action"] = action
    else:
        issues.append(_invalid_value(["action"], ("accept", "decline", "cancel")))
    content = value.get("content")
    if content is not None:  # null content counts as none (the schema's preprocess)
        if isinstance(content, dict):
            parsed["content"] = content
            issues.extend(_content_issues(content))
        else:
            issues.append(zodlike.type_issue(["content"], "record", content, present=True))
    return _loose(value, ("_meta", "action", "content"), parsed), issues


def sampling_content(
    value: JsonValue, *, present: bool = True
) -> tuple[JsonValue, list[JsonObject]]:
    """``SamplingContentSchema`` (text, image or audio): fields in schema order, others dropped."""
    if not isinstance(value, dict):
        issue: JsonObject = {
            "code": "invalid_type",
            "expected": "object",
            "path": ["content"],
            "message": f"Invalid input: expected object, received "
            f"{jsvalues.type_name(value) if present else 'undefined'}",
        }
        return None, [issue]
    kind = value.get("type")
    fields = {"text": ("text",), "image": ("data", "mimeType"), "audio": ("data", "mimeType")}
    if kind not in fields:
        return None, [
            {
                "code": "invalid_union",
                "errors": [],
                "note": "No matching discriminator",
                "discriminator": "type",
                "path": ["content", "type"],
                "message": "Invalid input",
            }
        ]
    parsed: JsonObject = {"type": kind}
    issues: list[JsonObject] = []
    for name in fields[str(kind)]:
        item = value.get(name)
        if isinstance(item, str):
            parsed[name] = item
        else:
            issues.append(
                zodlike.type_issue(["content", name], "string", item, present=name in value)
            )
    for name in ("annotations", "_meta"):
        if name in value:
            parsed[name] = value[name]
    return parsed, issues


def sampling_result(value: JsonValue) -> tuple[JsonObject, list[JsonObject]]:
    """``CreateMessageResultSchema.parse``: the parsed result and zod's issues."""
    if not isinstance(value, dict):
        return {}, [zodlike.type_issue([], "object", value, present=value is not None)]
    parsed: JsonObject = {}
    issues: list[JsonObject] = []
    if "_meta" in value:
        parsed["_meta"] = value["_meta"]
    model = value.get("model")
    if isinstance(model, str):
        parsed["model"] = model
    else:
        issues.append(zodlike.type_issue(["model"], "string", model, present="model" in value))
    if "stopReason" in value:
        parsed["stopReason"] = value["stopReason"]
    role = value.get("role")
    if role in ("user", "assistant"):
        parsed["role"] = role
    else:
        issues.append(_invalid_value(["role"], ("user", "assistant")))
    content, problems = sampling_content(value.get("content"), present="content" in value)
    issues += problems
    if content is not None and not problems:
        parsed["content"] = content
    return _loose(value, ("_meta", "model", "stopReason", "role", "content"), parsed), issues
