"""simulate-research-query's task timeline on a manual clock (timings: 000277-000297, 000411)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest
from fauxcall.clock import ManualClock
from fauxcall.runtime import RpcError
from mcp_session import dig

from fauxcall_everything import tasks
from fauxcall_everything.tasks import STAGE_NS, ResearchTask

if TYPE_CHECKING:
    from fauxcall.runtime import Channel
    from fauxcall.types import JsonValue

START = 1_792_024_202_000_000_000
SECOND = 1_000_000_000


class RecordingChannel:
    """A client's GET stream that notes what it receives; pauses move the manual clock."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.pushed: list[tuple[str, JsonValue]] = []

    def push(self, method: str, params: JsonValue = None) -> bool:
        self.pushed.append((method, params))
        return True

    async def pause(self, seconds: float) -> None:
        self.clock.advance(seconds)
        await asyncio.sleep(0)


def test_plain_task_goes_through_four_stages() -> None:
    task = ResearchTask("t" * 32, "coral", ambiguous=False, created_ns=START)
    seen = [(s.status, s.message) for s in task.statuses()]
    assert seen == [
        ("working", "Gathering sources..."),
        ("working", "Analyzing content..."),
        ("working", "Synthesizing findings..."),
        ("working", "Generating report..."),
        ("completed", "Generating report..."),
    ]
    assert task.at(START - SECOND).message == "Gathering sources..."  # a clock set back
    assert task.at(START + 2 * STAGE_NS).message == "Synthesizing findings..."
    assert task.ended_ns() == START + 4 * STAGE_NS
    json = task.to_json(task.at(START + 5 * SECOND))
    assert json["status"] == "completed"
    assert json["createdAt"] == tasks.iso_ms(START)
    assert not task.gone(START + 4 * STAGE_NS + 299 * SECOND)
    assert task.gone(START + 4 * STAGE_NS + 300 * SECOND)


def test_ambiguous_task_waits_then_times_out() -> None:
    task = ResearchTask("a" * 32, "Mercury", ambiguous=True, created_ns=START)
    assert task.at(START + 3 * SECOND).status == "input_required"
    assert task.ended_ns() is None
    assert not task.gone(START + 299 * SECOND)
    task.settle(START + 63 * SECOND)
    assert task.clarification == tasks.UNAVAILABLE
    assert task.resumed_ns == task.asks_at_ns + tasks.ELICITATION_TIMEOUT_NS
    assert task.at(task.resumed_ns + 3 * SECOND).status == "completed"
    assert "Elicitation failed" in str(dig(task.report(), "content", 0, "text"))


def test_a_task_that_never_ends_is_forgotten_after_its_ttl() -> None:
    task = ResearchTask("b" * 32, "x", ambiguous=True, created_ns=START)
    task.resume("slow", START + 400 * SECOND)
    assert task.gone(START + 300 * SECOND)


def test_cancelled_task() -> None:
    task = ResearchTask("c" * 32, "volcanoes", ambiguous=False, created_ns=START)
    task.cancelled_ns = START + SECOND // 2
    assert [s.message for s in task.statuses()] == ["Gathering sources..."]
    status = task.at(START + SECOND)
    assert (status.status, status.message) == ("cancelled", tasks.CANCELLED_MESSAGE)
    assert task.ended_ns() == task.cancelled_ns


@pytest.mark.parametrize(
    ("reply", "clarification", "fails"),
    [
        ({"action": "accept", "content": {"interpretation": "snake"}}, "snake", False),
        ({"action": "accept", "content": {"interpretation": ["a", "b"]}}, "a,b", False),
        ({"action": "accept", "content": {"interpretation": 7}}, "7", True),  # 000504
        ({"action": "accept", "content": {"interpretation": True}}, "true", True),
        ({"action": "accept", "content": {}}, "User accepted without selection", False),
        ({"action": "accept"}, "User cancelled - using default interpretation", False),
        ({"action": "decline"}, "User declined - using default interpretation", False),
        ({"action": "cancel"}, "User cancelled - using default interpretation", False),
        ({"action": "maybe"}, tasks.UNAVAILABLE, False),
    ],
)
def test_clarifications(reply: JsonValue, clarification: str, *, fails: bool) -> None:
    """A clarification that is neither text nor a list makes the report throw (000505)."""
    assert tasks.clarification_of(reply) == (clarification, fails)


def test_a_report_that_throws_fails_the_task() -> None:
    task = ResearchTask("t" * 32, "Python", ambiguous=True, created_ns=START)
    task.resume("5", START + 3 * SECOND, fails=True)
    last = task.statuses()[-1]
    assert (last.status, last.message) == ("failed", tasks.REPORT_FAILURE)
    assert last.at_ns == START + 3 * SECOND + 2 * STAGE_NS
    assert task.ended_ns() == last.at_ns
    assert task.at(last.at_ns).status == "failed"


def test_interpretations_depend_on_python() -> None:
    assert tasks.interpretations("Monty PYTHON")[1] == {
        "const": "snake",
        "title": "Python snake species",
    }
    assert tasks.interpretations("Jaguar")[0] == {
        "const": "technical",
        "title": "Technical/scientific perspective",
    }


def test_listing_pages_by_ten() -> None:
    created = [
        ResearchTask(f"{i:032x}", "t", ambiguous=False, created_ns=START + i) for i in range(12)
    ]
    first = tasks.page(created, None, START + SECOND)
    assert len(first["tasks"]) == 10  # type: ignore[arg-type]
    assert first["nextCursor"] == created[9].task_id
    assert list(first) == ["tasks", "nextCursor", "_meta"]
    second = tasks.page(created, created[9].task_id, START + SECOND)
    listed = [dig(t, "taskId") for t in cast("list[JsonValue]", second["tasks"])]
    assert listed == [created[10].task_id, created[11].task_id]
    assert "nextCursor" not in second
    with pytest.raises(RpcError) as refused:
        tasks.page(created, "nope", START)
    assert refused.value.message == "MCP error -32602: Failed to list tasks: Invalid cursor: nope"


async def test_status_pushes_follow_the_stages() -> None:
    clock = ManualClock(START)
    channel = RecordingChannel(clock)
    task = ResearchTask("d" * 32, "coral", ambiguous=False, created_ns=START)
    await tasks.push_statuses(task, clock.now_ns, cast("Channel", channel))
    statuses = [cast("dict[str, JsonValue]", params)["status"] for _, params in channel.pushed]
    assert statuses == ["working"] * 4 + ["completed"]
    assert {method for method, _ in channel.pushed} == {"notifications/tasks/status"}


async def test_status_pushes_wait_for_the_clarification() -> None:
    clock = ManualClock(START)
    channel = RecordingChannel(clock)
    task = ResearchTask("e" * 32, "Python", ambiguous=True, created_ns=START)
    pushing = asyncio.ensure_future(
        tasks.push_statuses(task, clock.now_ns, cast("Channel", channel))
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert [cast("dict[str, JsonValue]", p)["status"] for _, p in channel.pushed][-2:] == [
        "input_required"
    ] * 2
    task.resume("snake", clock.now_ns())
    await asyncio.wait_for(pushing, 1)
    messages = [cast("dict[str, JsonValue]", p)["statusMessage"] for _, p in channel.pushed]
    assert messages[-3:] == [
        'Continuing with interpretation: "snake"...',
        "Generating report...",
        "Generating report...",
    ]


async def test_status_pushes_go_on_after_an_unanswered_question() -> None:
    clock = ManualClock(START)
    channel = RecordingChannel(clock)
    task = ResearchTask("f" * 32, "Mercury", ambiguous=True, created_ns=START)
    pushing = asyncio.ensure_future(
        tasks.push_statuses(task, clock.now_ns, cast("Channel", channel))
    )
    for _ in range(20):
        await asyncio.sleep(0)
    clock.advance(61)
    task.answered.set()  # wake the wait, as the question's timeout would
    await asyncio.wait_for(pushing, 1)
    assert task.clarification == tasks.UNAVAILABLE


async def test_status_pushes_stop_at_cancellation() -> None:
    clock = ManualClock(START)
    channel = RecordingChannel(clock)
    task = ResearchTask("9" * 32, "x", ambiguous=False, created_ns=START)
    task.cancelled_ns = START + SECOND
    await tasks.push_statuses(task, clock.now_ns, cast("Channel", channel))
    assert len(channel.pushed) == 1


async def test_status_pushes_end_with_a_reset_of_the_fake() -> None:
    """A task a reset forgot pushes nothing more (``current`` turned false)."""
    clock = ManualClock(START)
    channel = RecordingChannel(clock)
    task = ResearchTask("8" * 32, "reefs", ambiguous=False, created_ns=START)
    turns = iter([True])  # current for one turn of the loop; then the fake is reset

    def current() -> bool:
        return next(turns, False)

    await asyncio.wait_for(
        tasks.push_statuses(task, clock.now_ns, cast("Channel", channel), current), 1
    )
    assert [cast("dict[str, JsonValue]", p)["statusMessage"] for _, p in channel.pushed] == [
        "Gathering sources..."
    ]
