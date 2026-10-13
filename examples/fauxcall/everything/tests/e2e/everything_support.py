"""Helpers shared by the Everything scenarios (both sides: the fake and the real server)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import TYPE_CHECKING, Any, Final

import pytest
from fauxcall.model.fields import NowPolicy, VolatilePolicy
from fauxcall.testing import CompareOptions
from fauxcall.timefmt import infer_time_format

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonValue

ISO_MS: Final = infer_time_format(["2026-10-12T22:19:57.081Z"])
LOCAL_TIME: Final = re.compile(r"\d{1,2}:\d{2}:\d{2} [AP]M$")


def now_fields(*paths: str) -> CompareOptions:
    """Time fields that must be close to each side's response time."""
    assert ISO_MS is not None
    return CompareOptions(policies=[NowPolicy(path=p, scope="body", format=ISO_MS) for p in paths])


def volatile(*paths: str) -> CompareOptions:
    """Fields that embed the server's wall clock (``created at 6:17:52 PM``): compared by type.

    Each scenario still checks their wording itself, on both sides.
    """
    return CompareOptions(policies=[VolatilePolicy(path=p, scope="body") for p in paths])


def zod(tool: str, detail: str) -> str:
    """The text of the SDK's refusal of invalid tool arguments."""
    return f"MCP error -32602: Input validation error: Invalid arguments for tool {tool}: {detail}"


def text_of(result: Any, index: int = 0) -> str:
    """The text of one content block of a tool result."""
    text: str = result["content"][index]["text"]
    return text


async def call_with(
    mcp: McpClient,
    name: str,
    params: dict[str, Any],
    *,
    compare: CompareOptions | None = None,
) -> Any:
    """``tools/call`` with extra params (``task``, ``_meta``), under the same write guard.

    ``McpClient.call_tool`` only sends ``name`` and ``arguments``; this sends more but spends the
    user's approval for a tool that may modify state exactly as ``call_tool`` does.
    """
    if mcp.target == "real" and mcp.mutating(name):
        if not mcp.allow_writes:
            pytest.skip(f"tool {name!r} may modify state: needs --allow-real-writes")
        refused = mcp.budget.spend("POST", f"tools/call:{name}") if mcp.budget else None
        if refused is not None:
            pytest.skip(f"real write refused: {refused}")
    if compare is None and mcp.varies(name):
        compare = CompareOptions(values=False, lengths=False)
    return await mcp.request(
        "tools/call", {"name": name, **params}, label=f"tools/call:{name}", compare=compare
    )


async def wait_for_task(mcp: McpClient, task_id: str, *, timeout_s: float = 30.0) -> Any:
    """Poll ``tasks/get`` until the task leaves ``working`` (identical polls count once)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        task: Any = await mcp.request(
            "tasks/get",
            {"taskId": task_id},
            label="tasks/get",
            compare=now_fields("$.createdAt", "$.lastUpdatedAt"),
        )
        if task["status"] != "working" or asyncio.get_running_loop().time() > deadline:
            return task
        await asyncio.sleep(1.0)


async def read_stream_messages(
    mcp: McpClient, trigger: Any, *, wanted: int, timeout_s: float = 3.0
) -> list[dict[str, JsonValue]] | None:
    """Open the client's GET stream, run ``trigger()``, and collect the first pushed messages.

    The in-process fake under test can't stream a GET response: on that side the trigger runs
    (its calls are compared) and ``None`` comes back; the unit tests check the fake's pushes.
    """
    if mcp.target != "real":
        await trigger()
        return None
    wire = mcp.wire
    headers = {"accept": "text/event-stream"}
    if wire.session_id:
        headers["mcp-session-id"] = wire.session_id
    if wire.negotiated_version:
        headers["mcp-protocol-version"] = wire.negotiated_version
    messages: list[dict[str, JsonValue]] = []

    async def collect() -> None:
        async with wire.http.stream("GET", wire.url, headers=headers, timeout=timeout_s) as stream:
            started = asyncio.ensure_future(trigger())
            async for line in stream.aiter_lines():
                if line.startswith("data:"):
                    messages.append(json.loads(line[5:].strip()))
                    if len(messages) >= wanted:
                        break
            await started

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(collect(), timeout_s)
    return messages
