"""The real server's argument checks, worded as zod 4 words them (recorded, not invented).

The server validates tool and prompt arguments with zod and the MCP SDK joins the issues with
newlines, each as ``<message> at <path>`` (recordings 000070-000095, 000104-000108, 000133-000137,
000422-000434). Unknown arguments are dropped, ``undefined`` takes a field's default (``null``
does not), and enum fields answer ``Invalid option`` whatever the received type.
Requests the SDK refuses before any handler runs answer ``-32603`` with zod's issue list as JSON
(``JSON.stringify(issues, null, 2)``: 000011, 000406, 000436, 000202).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from fauxcall.runtime import RpcError

from fauxcall_everything import jsvalues, urls

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue

Kind = Literal["string", "number", "boolean", "enum", "url"]
INTERNAL_ERROR: Final = -32603
INVALID_PARAMS: Final = -32602


class _Missing:
    """No default (distinct from a ``None`` default)."""


MISSING: Final = _Missing()


@dataclass(frozen=True, slots=True)
class Arg:
    """One argument of a tool or prompt, as its zod schema declares it."""

    name: str
    kind: Kind = "string"
    default: JsonValue | _Missing = MISSING
    optional: bool = False
    options: tuple[str, ...] = field(default_factory=tuple)
    minimum: float | None = None
    maximum: float | None = None


def _type_issue(expected: str, value: JsonValue, *, present: bool) -> str:
    received = jsvalues.type_name(value) if present else "undefined"
    return f"Invalid input: expected {expected}, received {received}"


def _issue(  # noqa: C901, PLR0911 - one branch per zod check, worded as zod words it
    arg: Arg, value: JsonValue, *, present: bool
) -> str | None:
    """The zod message for one value, ``None`` when it passes."""
    match arg.kind:
        case "enum":
            if not present or not isinstance(value, str) or value not in arg.options:
                choices = "|".join(json.dumps(option) for option in arg.options)
                return f"Invalid option: expected one of {choices}"
            return None
        case "number":
            if not present or isinstance(value, bool) or not isinstance(value, int | float):
                return _type_issue("number", value, present=present)
            if arg.minimum is not None and value < arg.minimum:
                return f"Too small: expected number to be >={jsvalues.number_text(arg.minimum)}"
            if arg.maximum is not None and value > arg.maximum:
                return f"Too big: expected number to be <={jsvalues.number_text(arg.maximum)}"
            return None
        case "boolean":
            if not present or not isinstance(value, bool):
                return _type_issue("boolean", value, present=present)
            return None
        case _:
            if not present or not isinstance(value, str):
                return _type_issue("string", value, present=present)
            if arg.kind == "url" and not urls.is_url(jsvalues.trim(value)):
                return "Invalid URL"
            return None


def url_value(value: JsonValue) -> JsonValue:
    """What zod's ``url()`` hands the tool: trimmed, tabs and line breaks gone (000490-000493)."""
    trimmed = jsvalues.trim(str(value))
    return trimmed.replace("\t", "").replace("\n", "").replace("\r", "")


def check(args: tuple[Arg, ...], arguments: JsonObject) -> tuple[JsonObject, list[str]]:
    """Validate like ``schema.parse``: the parsed arguments and the issues (``at <path>``)."""
    parsed: JsonObject = {}
    issues: list[str] = []
    for arg in args:
        present = arg.name in arguments
        if not present and not isinstance(arg.default, _Missing):
            parsed[arg.name] = arg.default
            continue
        if not present and arg.optional:
            continue
        value = arguments.get(arg.name)
        problem = _issue(arg, value, present=present)
        if problem is None:
            parsed[arg.name] = url_value(value) if arg.kind == "url" else value
        else:
            issues.append(f"{problem} at {arg.name}")
    return parsed, issues


def tool_error(text: str) -> JsonObject:
    """A tool's failure as the SDK reports it: an ``isError`` result."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


def invalid_tool_arguments(tool: str, issues: list[str]) -> JsonObject:
    """The ``isError`` result for arguments that break a tool's schema."""
    joined = "\n".join(issues)
    return tool_error(
        f"MCP error -32602: Input validation error: Invalid arguments for tool {tool}: {joined}"
    )


def invalid_prompt_arguments(prompt: str, issues: list[str]) -> RpcError:
    """The JSON-RPC error for arguments that break a prompt's schema."""
    joined = "\n".join(issues)
    return RpcError(
        INVALID_PARAMS, f"MCP error -32602: Invalid arguments for prompt {prompt}: {joined}"
    )


def type_issue(
    path: list[str | int], expected: str, value: JsonValue, *, present: bool
) -> JsonObject:
    """One ``invalid_type`` issue of a request the SDK refuses, in zod's member order."""
    return {
        "expected": expected,
        "code": "invalid_type",
        "path": list(path),
        "message": _type_issue(expected, value, present=present),
    }


def request_error(issues: list[JsonObject]) -> RpcError:
    """A request refused by the SDK's schema: ``-32603`` with zod's issues as JSON."""
    return RpcError(INTERNAL_ERROR, json.dumps(issues, indent=2, ensure_ascii=False))


def require_string(params: JsonObject, name: str) -> str:
    """``params.<name>`` as a string, else the SDK's refusal of the request."""
    value = params.get(name)
    if isinstance(value, str):
        return value
    raise request_error([type_issue(["params", name], "string", value, present=name in params)])
