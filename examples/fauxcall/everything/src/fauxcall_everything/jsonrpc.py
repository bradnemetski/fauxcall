"""The SDK's JSON-RPC message schemas, as its transport checks every message posted to it.

``JSONRPCMessageSchema`` (MCP TypeScript SDK, ``types.js``) is a union of four strict objects: a
request, a notification, a result and an error. A POST holding anything else is refused with HTTP
400 ``Parse error: Invalid JSON-RPC message`` before any session or MCP handling: a progress token
that is neither a string nor an integer is enough (000448, 000449). Only a message
``InitializeRequestSchema`` accepts opens a session: any other ``initialize`` is a request before
initialization, refused as such (000511 has no ``clientInfo``, 000512 a numeric version).

Numbers follow zod: ``z.number()`` is finite, ``.int()`` a safe integer (within ±(2^53 - 1)).
``optional()`` means absent, never ``null``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from fauxcall_everything.jsvalues import MAX_SAFE_INTEGER, is_number, to_double
from fauxcall_everything.zodish import type_issue, value_issue

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from fauxcall.types import JsonObject, JsonValue

    Check = Callable[[object], bool]
    Issues = Callable[[object, list[JsonValue]], list[JsonObject]]

RELATED_TASK: Final = "io.modelcontextprotocol/related-task"
_ROLES: Final = {
    "request": frozenset({"jsonrpc", "id", "method", "params"}),
    "notification": frozenset({"jsonrpc", "method", "params"}),
    "result": frozenset({"jsonrpc", "id", "result"}),
    "error": frozenset({"jsonrpc", "id", "error"}),
}
"""The members each strict message object allows."""


def is_string(value: object) -> bool:
    """``z.string()``."""
    return isinstance(value, str)


def is_boolean(value: object) -> bool:
    """``z.boolean()``."""
    return isinstance(value, bool)


def is_safe_integer(value: object) -> bool:
    """``z.number().int()``: a whole number within ±(2^53 - 1)."""
    if not is_number(value):
        return False
    number = to_double(value)  # type: ignore[arg-type]
    return math.isfinite(number) and number.is_integer() and abs(number) <= MAX_SAFE_INTEGER


def is_some_object(value: object) -> bool:
    """The SDK's ``AssertObjectSchema``: anything ``typeof`` calls an object (arrays too)."""
    return isinstance(value, dict | list)


def _fits(value: Mapping[str, object], fields: Mapping[str, Check]) -> bool:
    """Every optional field ``value`` has passes its check (other members are allowed)."""
    return all(name not in value or check(value[name]) for name, check in fields.items())


def _object(fields: Mapping[str, Check], required: Mapping[str, Check] | None = None) -> Check:
    """``z.object({...})`` (or ``z.looseObject``): an object whose known fields fit."""

    def check(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        needed = required or {}
        present = all(name in value and test(value[name]) for name, test in needed.items())
        return present and _fits(value, fields)

    return check


def _record(values: Check) -> Check:
    """``z.record(z.string(), values)``."""
    return lambda value: isinstance(value, dict) and all(values(item) for item in value.values())


def _array(items: Check) -> Check:
    """``z.array(items)``."""
    return lambda value: isinstance(value, list) and all(items(item) for item in value)


def _request_id(value: object) -> bool:
    """``RequestIdSchema``: a string or an integer."""
    return is_string(value) or is_safe_integer(value)


_meta: Final = _object(
    {
        "progressToken": _request_id,  # ProgressTokenSchema: a string or an integer too
        RELATED_TASK: _object({}, {"taskId": is_string}),
    }
)
"""``RequestMetaSchema``."""

_params: Final = _object({"_meta": _meta})
"""``BaseRequestParamsSchema.loose()`` (and the notifications' params, alike)."""

_error: Final = _object(
    {"data": lambda _value: True}, {"code": is_safe_integer, "message": is_string}
)


def _role_of(message: Mapping[str, object]) -> str | None:
    """Which of the four message objects ``message`` is meant as (its members decide)."""
    if "method" in message:
        return "request" if "id" in message else "notification"
    if "result" in message:
        return "result"
    return "error" if "error" in message else None


def is_message(value: JsonValue) -> bool:
    """Whether ``JSONRPCMessageSchema`` accepts ``value``."""
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        return False
    role = _role_of(value)
    if role is None or not set(value) <= _ROLES[role]:
        return False
    match role:
        case "request":
            fits = _request_id(value["id"]) and is_string(value["method"])
            return fits and _fits(value, {"params": _params})
        case "notification":
            return is_string(value["method"]) and _fits(value, {"params": _params})
        case "result":
            return _request_id(value["id"]) and _object({"_meta": _meta})(value["result"])
        case _:
            return _fits(value, {"id": _request_id}) and _error(value["error"])


_icon: Final = _object(
    {
        "mimeType": is_string,
        "sizes": _array(is_string),
        "theme": lambda value: isinstance(value, str) and value in {"light", "dark"},
    },
    {"src": is_string},
)
_implementation: Final = _object(
    {
        "title": is_string,
        "icons": _array(_icon),
        "websiteUrl": is_string,
        "description": is_string,
    },
    {"name": is_string, "version": is_string},
)
"""``ImplementationSchema`` (the client's ``clientInfo``)."""


def _elicitation(value: object) -> bool:
    """``ElicitationCapabilitySchema``: ``{}`` means form; ``form`` and ``url`` are objects."""
    form = _object({"applyDefaults": is_boolean})
    return _object({"form": form, "url": is_some_object})(value)


_capabilities: Final = _object(
    {
        "experimental": _record(is_some_object),
        "sampling": _object({"context": is_some_object, "tools": is_some_object}),
        "elicitation": _elicitation,
        "roots": _object({"listChanged": is_boolean}),
        "tasks": _object(
            {
                "list": is_some_object,
                "cancel": is_some_object,
                "requests": _object(
                    {
                        "sampling": _object({"createMessage": is_some_object}),
                        "elicitation": _object({"create": is_some_object}),
                    }
                ),
            }
        ),
        "extensions": _record(is_some_object),
    }
)
"""``ClientCapabilitiesSchema``."""

_initialize_params: Final = _object(
    {"_meta": _meta},
    {"protocolVersion": is_string, "capabilities": _capabilities, "clientInfo": _implementation},
)


def is_initialize_request(message: JsonValue) -> bool:
    """Whether ``InitializeRequestSchema`` accepts ``message`` (a valid handshake)."""
    return (
        isinstance(message, dict)
        and message.get("method") == "initialize"
        and "params" in message
        and _initialize_params(message["params"])
    )


# -- the handshake's issues, as zod reports them --------------------------------------------------

_ABSENT: Final = object()


def _scalar(expected: str, test: Check) -> Issues:
    def issues(value: object, path: list[JsonValue]) -> list[JsonObject]:
        if test(value):
            return []
        return [type_issue(expected, value, path, present=value is not _ABSENT)]

    return issues


def _some_object(value: object, path: list[JsonValue]) -> list[JsonObject]:
    """``AssertObjectSchema`` is a ``z.custom`` check: zod's generic refusal."""
    return (
        []
        if is_some_object(value)
        else [{"code": "custom", "path": path, "message": "Invalid input"}]
    )


def _fields(fields: Mapping[str, Issues], required: frozenset[str] = frozenset()) -> Issues:
    """``z.object(shape)``: each field's issues in the shape's order (absent optional: none)."""

    def issues(value: object, path: list[JsonValue]) -> list[JsonObject]:
        if not isinstance(value, dict):
            return [type_issue("object", value, path, present=value is not _ABSENT)]
        found: list[JsonObject] = []
        for name, check in fields.items():
            if name in value:
                found += check(value[name], [*path, name])
            elif name in required:
                found += check(_ABSENT, [*path, name])
        return found

    return issues


def _record_of(values: Issues) -> Issues:
    def issues(value: object, path: list[JsonValue]) -> list[JsonObject]:
        if not isinstance(value, dict):
            return [type_issue("record", value, path, present=value is not _ABSENT)]
        return [i for key, item in value.items() for i in values(item, [*path, key])]

    return issues


def _array_of(items: Issues) -> Issues:
    def issues(value: object, path: list[JsonValue]) -> list[JsonObject]:
        if not isinstance(value, list):
            return [type_issue("array", value, path, present=value is not _ABSENT)]
        return [i for index, item in enumerate(value) for i in items(item, [*path, index])]

    return issues


def _intersection(left: Issues) -> Issues:
    """``z.intersection(left, z.record(...))``: both sides' issues."""
    right = _record_of(lambda _value, _path: [])

    def issues(value: object, path: list[JsonValue]) -> list[JsonObject]:
        return left(value, path) + right(value, path)

    return issues


def _theme(value: object, path: list[JsonValue]) -> list[JsonObject]:
    return (
        []
        if value in {"light", "dark"} and isinstance(value, str)
        else [value_issue(["light", "dark"], path)]
    )


_STRING: Final = _scalar("string", is_string)
_BOOLEAN: Final = _scalar("boolean", is_boolean)
_FORM: Final = _intersection(_fields({"applyDefaults": _BOOLEAN}))
_HANDSHAKE: Final = _fields(
    {
        "protocolVersion": _STRING,
        "capabilities": _fields(
            {
                "experimental": _record_of(_some_object),
                "sampling": _fields({"context": _some_object, "tools": _some_object}),
                "elicitation": _intersection(_fields({"form": _FORM, "url": _some_object})),
                "roots": _fields({"listChanged": _BOOLEAN}),
                "tasks": _fields(
                    {
                        "list": _some_object,
                        "cancel": _some_object,
                        "requests": _fields(
                            {
                                "sampling": _fields({"createMessage": _some_object}),
                                "elicitation": _fields({"create": _some_object}),
                            }
                        ),
                    }
                ),
                "extensions": _record_of(_some_object),
            }
        ),
        "clientInfo": _fields(
            {
                "name": _STRING,
                "title": _STRING,
                "icons": _array_of(
                    _fields(
                        {
                            "src": _STRING,
                            "mimeType": _STRING,
                            "sizes": _array_of(_STRING),
                            "theme": _theme,
                        },
                        frozenset({"src"}),
                    )
                ),
                "version": _STRING,
                "websiteUrl": _STRING,
                "description": _STRING,
            },
            frozenset({"name", "version"}),
        ),
    },
    frozenset({"protocolVersion", "capabilities", "clientInfo"}),
)


def initialize_issues(message: JsonObject) -> list[JsonObject]:
    """Why ``InitializeRequestSchema`` refuses an ``initialize`` (000514-000517).

    Inside a session that is not a handshake but a request like any other: its handler parses it
    and answers -32603 with these issues, depth first in the schemas' field order (000516).
    """
    params: object = message.get("params", _ABSENT)
    return _HANDSHAKE(params, ["params"])
