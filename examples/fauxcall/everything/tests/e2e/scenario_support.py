"""Helpers for the scenarios: reading answers, and how parity compares what legitimately varies.

What varies between the real server and the fake, run to run, and how it is compared:

- the local time stamped on dynamic resources (``created at 9:21:01 PM``, also inside their
  base64 blobs): by type and length;
- MCP session ids in texts, random elicitation ids: by type and length;
- task ids: by type (and later calls through them, as parity does for made-up ids);
- task timestamps: as instants close to the response time.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import pytest
from fauxcall.model.fields import NowPolicy, RandomPolicy
from fauxcall.testing import CompareOptions
from fauxcall.timefmt import TimeFormat

if TYPE_CHECKING:
    from fauxcall.model.fields import FieldPolicy
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject, JsonValue

PLAIN_TOOLS: Final = [
    "echo",
    "get-annotated-message",
    "get-env",
    "get-resource-links",
    "get-resource-reference",
    "get-structured-content",
    "get-sum",
    "get-tiny-image",
    "gzip-file-as-resource",
    "toggle-simulated-logging",
    "toggle-subscriber-updates",
    "trigger-long-running-operation",
    "simulate-research-query",
]
"""The tools every client sees, in the server's order (000002)."""

ISO_MS: Final = TimeFormat(kind="iso", fraction_digits=3, tz_style="Z")
LOCAL_TIME: Final = re.compile(r"created at \d{1,2}:\d\d:\d\d [AP]M$")
UUID: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def varying(*paths: str) -> CompareOptions:
    """Compare these body paths by type and length (generalized: ``$.content[*].text``)."""
    policies: list[FieldPolicy] = [
        RandomPolicy(path=path, scope="body", generator="uuid4") for path in paths
    ]
    return CompareOptions(policies=policies)


STAMPED: Final = varying(
    "$.content[*].resource.text",
    "$.content[*].resource.blob",
    "$.contents[*].text",
    "$.contents[*].blob",
    "$.messages[*].content.resource.text",
    "$.messages[*].content.resource.blob",
)
"""Answers carrying a dynamic resource, stamped with the server's local time."""

TEXTS: Final = varying("$.content[*].text")
"""Answers whose texts carry a session or elicitation id."""


def task_times(prefix: str = "$") -> CompareOptions:
    """A task's timestamps are instants near the response; its id is made up by the server."""
    policies: list[FieldPolicy] = [
        NowPolicy(path=f"{prefix}.createdAt", scope="body", format=ISO_MS),
        NowPolicy(path=f"{prefix}.lastUpdatedAt", scope="body", format=ISO_MS),
        RandomPolicy(path=f"{prefix}.taskId", scope="body", generator="hex", length=32),
    ]
    return CompareOptions(policies=policies)


def dig(value: JsonValue, *path: str | int) -> JsonValue:
    """A member deep inside an answer (asserting each step exists)."""
    for step in path:
        if isinstance(step, int):
            assert isinstance(value, list), f"{step}: not in {value!r}"
            value = value[step]
        else:
            assert isinstance(value, dict), f"{step}: not in {value!r}"
            value = value[step]
    return value


def text(value: JsonValue, index: int = 0) -> str:
    """The text of one content block of a tool result."""
    return str(dig(value, "content", index, "text"))


def texts(value: JsonValue) -> list[str]:
    """The texts of every content block."""
    blocks = dig(value, "content")
    assert isinstance(blocks, list)
    return [str(dig(block, "text")) for block in blocks if dig(block, "type") == "text"]


async def tool_call(
    mcp: McpClient, params: JsonObject, *, compare: CompareOptions | None = None
) -> JsonValue:
    """A ``tools/call`` with more than a name and arguments (``task``, ``_meta`` …).

    It spends the up-front approval exactly as ``mcp.call_tool`` does: on the real server, a
    tool that may modify state needs ``--allow-real-writes`` and budget left, else the scenario
    is skipped.
    """
    name = str(params.get("name"))
    if mcp.target == "real" and mcp.mutating(name):
        if not mcp.allow_writes:
            pytest.skip(f"tool {name!r} may modify state: needs --allow-real-writes")
        refused = mcp.budget.spend("POST", f"tools/call:{name}") if mcp.budget else None
        if refused is not None:
            pytest.skip(f"real write refused: {refused}")
    return await mcp.request("tools/call", params, compare=compare)
