"""Helpers shared by the scenarios."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import pytest
from fauxcall.model.fields import VolatilePolicy
from fauxcall.testing.client import CompareOptions

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

LOCAL_TIME: Final = re.compile(r"^\d{1,2}:\d\d:\d\d [AP]M$")
"""How the server prints its clock (toLocaleTimeString)."""
ISO_MS: Final = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
TASK_ID: Final = re.compile(r"^[0-9a-f]{32}$")
SHAPE_ONLY: Final = CompareOptions(values=False)
"""For answers whose every value depends on when they ran (real and fake run apart)."""


def stamped(*paths: str) -> CompareOptions:
    """Compare by value, except fields stamped with the server's clock (by type only)."""
    return CompareOptions(policies=[VolatilePolicy(path=path, scope="body") for path in paths])


STAMPED_READ: Final = stamped("$.contents[*].text", "$.contents[*].blob")
"""A dynamic resource: its text or blob carries the server's local time."""
STAMPED_PROMPT: Final = stamped(
    "$.messages[*].content.resource.text", "$.messages[*].content.resource.blob"
)
STAMPED_REFERENCE: Final = stamped("$.content[*].resource.text", "$.content[*].resource.blob")
STAMPED_TASK: Final = stamped(
    "$.task.createdAt",
    "$.task.lastUpdatedAt",
    "$.createdAt",
    "$.lastUpdatedAt",
    "$.tasks[*].createdAt",
    "$.tasks[*].lastUpdatedAt",
)
"""Task objects: their times come from the server's clock (task ids are matched by the harness)."""

ALL_CAPABILITIES: Final[dict[str, Any]] = {
    "sampling": {},
    "elicitation": {"form": {}, "url": {}},
    "roots": {"listChanged": True},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


def texts(result: Any) -> list[str]:
    """The text blocks of a tool result."""
    return [block["text"] for block in result["content"] if block.get("type") == "text"]


async def call_with(  # noqa: PLR0913 - mirrors McpClient.call_tool, plus the params
    mcp: McpClient,
    name: str,
    arguments: dict[str, Any],
    extra: dict[str, Any],
    *,
    label: str | None = None,
    compare: CompareOptions | None = None,
) -> Any:
    """``tools/call`` with more params (``task``, ``_meta``), under the same write rules.

    ``McpClient.call_tool`` sends only a name and arguments; a task-augmented call or a progress
    token needs the params themselves. Tools that may modify state still need
    ``--allow-real-writes`` and spend the approval budget on the real side, as ``call_tool``.
    """
    if mcp.target == "real" and mcp.mutating(name):
        if not mcp.allow_writes:
            pytest.skip(f"tool {name!r} may modify state: needs --allow-real-writes")
        refused = mcp.budget.spend("POST", f"tools/call:{name}") if mcp.budget else None
        if refused is not None:
            pytest.skip(f"real write refused: {refused}")
    params = {"name": name, "arguments": arguments, **extra}
    if compare is None and mcp.varies(name):
        compare = CompareOptions(values=False, lengths=False)
    return await mcp.request(
        "tools/call", params, label=label or f"tools/call:{name}", compare=compare
    )
