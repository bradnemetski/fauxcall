"""``trigger-long-running-operation``: progress notifications, timing, odd step counts, cancel."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from scenario_support import text_of

if TYPE_CHECKING:
    from fauxcall.testing import McpClient


async def test_progress_steps(mcp: McpClient) -> None:
    result: Any = await mcp.call_tool(
        "trigger-long-running-operation", {"duration": 0.6, "steps": 3}, progress=True
    )
    assert text_of(result) == ("Long running operation completed. Duration: 0.6 seconds, Steps: 3.")
    await mcp.call_tool(
        "trigger-long-running-operation", {"duration": 0.4, "steps": 2}, progress=True
    )


async def test_fractional_and_degenerate_steps(mcp: McpClient) -> None:
    await mcp.call_tool(
        "trigger-long-running-operation", {"duration": 0.3, "steps": 1.5}, progress=True
    )
    await mcp.call_tool(
        "trigger-long-running-operation", {"duration": 5, "steps": -1}, progress=True
    )
    await mcp.call_tool(
        "trigger-long-running-operation", {"duration": -2, "steps": 3}, progress=True
    )
    await mcp.call_tool("trigger-long-running-operation", {"duration": 0, "steps": 0})


async def test_without_a_progress_token_nothing_is_reported(mcp: McpClient) -> None:
    started = time.monotonic()
    result: Any = await mcp.call_tool(
        "trigger-long-running-operation", {"duration": 0.5, "steps": 2}, notifications=True
    )
    assert "Steps: 2." in text_of(result)
    assert time.monotonic() - started >= 0.45  # two 0.25 s steps, waited for real on both sides


async def test_steps_take_their_time(mcp: McpClient) -> None:
    started = time.monotonic()
    await mcp.call_tool(
        "trigger-long-running-operation", {"duration": 0.9, "steps": 3}, progress=True
    )
    assert 0.85 <= time.monotonic() - started < 3.0
    started = time.monotonic()
    await mcp.call_tool("trigger-long-running-operation", {"duration": 0, "steps": 100})
    assert time.monotonic() - started >= 0.1  # Node waits at least 1 ms per step


async def test_bad_arguments(mcp: McpClient) -> None:
    await mcp.call_tool("trigger-long-running-operation", {"duration": "1s"})
    await mcp.call_tool("trigger-long-running-operation", {"steps": [3]})
    await mcp.call_tool("trigger-long-running-operation", {"duration": None, "steps": True})


async def test_a_cancelled_operation_gets_no_answer(mcp: McpClient) -> None:
    # cancelled at 0.2 s, the work still runs to its end (progress at 0.3, 0.6 and 0.9 s, read
    # within the client's 1 s linger): the TypeScript SDK never stops a cancelled handler
    await mcp.call_tool(
        "trigger-long-running-operation",
        {"duration": 0.9, "steps": 3},
        progress=True,
        cancel_after=0.2,
    )
    assert await mcp.request("ping", {}) == {}
    echoed: Any = await mcp.call_tool("echo", {"message": "still serving"})
    assert text_of(echoed) == "Echo: still serving"
