"""Research tasks on the fake clock: stages, notifications, results, cancel, expiry, questions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mcp_session import Session, session

if TYPE_CHECKING:
    from fauxcall.types import JsonObject

TOOL = "simulate-research-query"
ELICIT: JsonObject = {"elicitation": {}}


async def _start(client: Session, topic: str, *, ambiguous: bool = False) -> str:
    created = await client.tool(TOOL, {"topic": topic, "ambiguous": ambiguous}, task={"ttl": 1})
    assert created["task"]["status"] == "working"
    assert created["task"]["ttl"] == 300000
    assert created["task"]["statusMessage"] == "Gathering sources..."
    return str(created["task"]["taskId"])


async def test_stages_follow_the_clock() -> None:
    async with session(listen=True, frozen=True) as client:
        task_id = await _start(client, "glaciers")
        messages = []
        stages = ("Analyzing content...", "Synthesizing findings...", "Generating report...")
        for expected in stages:
            await client.advance(1.0)
            task = await client.call("tasks/get", {"taskId": task_id})
            assert (task["status"], task["statusMessage"]) == ("working", expected)
            messages.append(expected)
        await client.advance(1.0)
        done = await client.call("tasks/get", {"taskId": task_id})
        assert done["status"] == "completed"
        assert done["statusMessage"] == "Generating report..."
        statuses = await client.heard("notifications/tasks/status", count=5)
        assert [m["params"]["status"] for m in statuses] == ["working"] * 4 + ["completed"]
        result = await client.call("tasks/result", {"taskId": task_id})
        assert result["_meta"] == {"io.modelcontextprotocol/related-task": {"taskId": task_id}}
        assert result["content"][0]["text"].startswith("# Research Report: glaciers\n")
        listing = await client.call("tasks/list", {})
        assert [t["taskId"] for t in listing["tasks"]] == [task_id]
        assert listing["_meta"] == {}
        refused = await client.call("tasks/cancel", {"taskId": task_id})
        assert refused["message"] == (
            "MCP error -32602: Cannot cancel task in terminal status: completed"
        )


async def test_result_waits_until_the_task_is_done() -> None:
    async with session(frozen=True) as client:
        task_id = await _start(client, "volcanoes")
        waiting = asyncio.ensure_future(client.call("tasks/result", {"taskId": task_id}))
        for _ in range(5):
            await client.advance(1.0)
        result = await asyncio.wait_for(waiting, 5)
        text = result["content"][0]["text"]
        assert "## Research Parameters\n- **Topic**: volcanoes\n\n\n" in text


async def test_cancel_stops_the_research() -> None:
    async with session(listen=True, frozen=True) as client:
        task_id = await _start(client, "rivers")
        cancelled = await client.call("tasks/cancel", {"taskId": task_id})
        assert next(iter(cancelled)) == "_meta"
        assert cancelled["status"] == "cancelled"
        assert cancelled["statusMessage"] == "Client cancelled task execution."
        await client.advance(2.0)
        task = await client.call("tasks/get", {"taskId": task_id})
        assert task["status"] == "cancelled"
        error = await client.call("tasks/result", {"taskId": task_id})
        assert error == {"code": -32603, "message": f"Task {task_id} has no result stored"}
        assert len(await client.heard("notifications/tasks/status", count=3, within=0.5)) == 1


async def test_tasks_expire_five_minutes_after_they_finish() -> None:
    async with session(frozen=True) as client:
        task_id = await _start(client, "deserts")
        await client.advance(299.0)
        assert (await client.call("tasks/get", {"taskId": task_id}))["status"] == "completed"
        await client.advance(10.0)
        gone = await client.call("tasks/get", {"taskId": task_id})
        assert gone["message"] == "MCP error -32602: Failed to retrieve task: Task not found"


async def test_list_pages_ten_at_a_time() -> None:
    async with session(frozen=True) as client:
        ids = [await _start(client, f"topic {index}") for index in range(12)]
        first = await client.call("tasks/list", {})
        assert [t["taskId"] for t in first["tasks"]] == ids[:10]
        assert first["nextCursor"] == ids[9]
        second = await client.call("tasks/list", {"cursor": first["nextCursor"]})
        assert [t["taskId"] for t in second["tasks"]] == ids[10:]
        assert "nextCursor" not in second
        bad = await client.call("tasks/list", {"cursor": "nope"})
        assert bad["message"] == "MCP error -32602: Failed to list tasks: Invalid cursor: nope"


async def test_task_errors() -> None:
    async with session() as client:
        missing = await client.call("tasks/get", {"taskId": "x"})
        assert missing["code"] == -32602
        assert (await client.call("tasks/result", {"taskId": "x"}))["message"] == (
            "MCP error -32602: Task not found: x"
        )
        assert (await client.call("tasks/cancel", {"taskId": "x"}))["message"] == (
            "MCP error -32602: Task not found: x"
        )
        protocol = await client.call("tasks/get", {})
        assert protocol["code"] == -32603
        assert '"taskId"' in protocol["message"]
        plain = await client.tool(TOOL, {"topic": "x"})
        assert plain["isError"] is True
        assert "requires task augmentation" in plain["content"][0]["text"]
        invalid = await client.tool(TOOL, {"topic": 1}, task={})
        assert invalid["message"].startswith("MCP error -32602: Invalid task creation result: [")


async def _clarified(reply: Any, topic: str = "Python") -> str:
    async with session(ELICIT, {"elicitation/create": reply}, listen=True, frozen=True) as client:
        task_id = await _start(client, topic, ambiguous=True)
        await client.advance(1.0)
        await client.advance(1.0)
        task = await client.call("tasks/get", {"taskId": task_id})
        assert task["status"] == "input_required"
        waiting = asyncio.ensure_future(client.messages("tasks/result", {"taskId": task_id}))
        for _ in range(4):
            await client.advance(1.0)
        messages = await asyncio.wait_for(waiting, 5)
        request = messages[0]
        assert request["method"] == "elicitation/create"
        assert request["params"]["_meta"] == {
            "io.modelcontextprotocol/related-task": {"taskId": task_id}
        }
        statuses = await client.heard("notifications/tasks/status", count=8)
        assert [m["params"]["status"] for m in statuses].count("input_required") == 2
        return str(messages[-1]["result"]["content"][0]["text"])


async def test_clarification_accepted() -> None:
    report = await _clarified({"action": "accept", "content": {"interpretation": "snake"}})
    assert report.startswith("# Research Report: Python (snake)\n")
    assert 'After receiving clarification ("snake")' in report


async def test_clarification_without_a_choice_declined_cancelled_or_failed() -> None:
    accepted = await _clarified({"action": "accept", "content": {}})
    assert "(User accepted without selection)" in accepted
    assert "(User declined - using default interpretation)" in await _clarified(
        {"action": "decline"}, "mercury"
    )
    assert "(User cancelled - using default interpretation)" in await _clarified(
        {"action": "cancel"}, "jaguar"
    )
    failed = await _clarified({"error": {"code": -1, "message": "no"}}, "amazon")
    assert "**Note:** Elicitation failed and a default interpretation was used." in failed
    refused = await _clarified({"action": "maybe"}, "java")
    assert "(technical (default - elicitation unavailable))" in refused


async def test_unanswered_clarification_times_out() -> None:
    async with session(ELICIT, frozen=True) as client:
        task_id = await _start(client, "python", ambiguous=True)
        await client.advance(1.0)
        await client.advance(1.0)
        await client.advance(61.0)
        await client.advance(1.0)
        await client.advance(1.0)
        result = await client.call("tasks/result", {"taskId": task_id})
        assert "(technical (default - elicitation unavailable))" in result["content"][0]["text"]


async def test_cancel_while_waiting_for_clarification() -> None:
    async with session(ELICIT, frozen=True) as client:
        task_id = await _start(client, "python", ambiguous=True)
        await client.advance(1.0)
        await client.advance(1.0)
        cancelled = await client.call("tasks/cancel", {"taskId": task_id})
        assert cancelled["status"] == "cancelled"
        await client.advance(2.0)
        assert (await client.call("tasks/get", {"taskId": task_id}))["status"] == "cancelled"


async def test_ambiguous_ignored_without_elicitation() -> None:
    async with session(frozen=True) as client:
        task_id = await _start(client, "python", ambiguous=True)
        for _ in range(4):
            await client.advance(1.0)
        result = await client.call("tasks/result", {"taskId": task_id})
        assert "Clarification" not in result["content"][0]["text"]
