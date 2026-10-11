from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from fauxcall.analysis.jobs import (
    JobTimeline,
    StateTiming,
    summarize,
    timelines,
)
from fauxcall.clock import datetime_to_ns
from fauxcall.model.jobs import DurationModel, JobModel, JobOutcome, JobStateModel
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange
from fauxcall.timefmt import NS_PER_SECOND
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

BASE_NS = datetime_to_ns(datetime(2026, 10, 9, 15, 0, tzinfo=UTC))
TERMINAL = ("done", "failed")


def poll(
    number: int,
    state: str | None,
    at_s: float,
    *,
    call: str = "submit",
    tagged: bool = True,
    response: bool = True,
) -> Exchange:
    started_ns = BASE_NS + round(at_s * NS_PER_SECOND)
    body: JsonValue = {"id": "j1"} if state is None else {"id": "j1", "status": state}
    return Exchange(
        id=f"{number:06d}",
        endpoint="GET /v1/jobs/{id}",
        purpose="job_poll",
        plan_call=call,
        started_at=datetime.fromtimestamp(started_ns / NS_PER_SECOND, UTC),
        started_ns=started_ns,
        request=CapturedRequest(
            method=HttpMethod.GET, url="https://api.test/v1/jobs/j1", path="/v1/jobs/j1"
        ),
        response=CapturedResponse(status=200, body=Body(kind="json", json_value=body))
        if response
        else None,
        tags=[f"poll:{call}"] if tagged else ["other"],
    )


def polls(
    states: list[str | None], *, start: int = 1, at: float = 0.0, call: str = "submit"
) -> list[Exchange]:
    return [poll(start + i, state, at + i, call=call) for i, state in enumerate(states)]


def test_single_job_bounds() -> None:
    (timeline,) = timelines(
        polls(["queued", "queued", "processing", "processing", "processing", "done"]),
        state_path="$.status",
        terminal=TERMINAL,
    )
    assert timeline.job == "submit"
    assert timeline.terminal == "done"
    assert timeline.observations[:3] == [(0.0, "queued"), (1.0, "queued"), (2.0, "processing")]
    assert timeline.states == [
        StateTiming(state="queued", min_s=1.0, max_s=2.0),
        StateTiming(state="processing", min_s=2.0, max_s=4.0),
    ]
    assert [s.estimate_s for s in timeline.states] == [1.5, 3.0]


def test_recurring_states_get_their_own_bounds() -> None:
    (timeline,) = timelines(
        polls(["queued", "processing", "retrying", "retrying", "processing", "failed"]),
        state_path="$.status",
        terminal=TERMINAL,
    )
    assert [(s.state, s.min_s, s.max_s) for s in timeline.states] == [
        ("queued", 0.0, 1.0),
        ("processing", 0.0, 2.0),
        ("retrying", 1.0, 3.0),
        ("processing", 0.0, 2.0),
    ]
    assert timeline.terminal == "failed"


def test_timeout_without_a_terminal_state() -> None:
    (timeline,) = timelines(
        polls(["queued", "processing", "processing"]), state_path="$.status", terminal=TERMINAL
    )
    assert timeline.terminal is None
    last = timeline.states[-1]
    assert (last.state, last.min_s, last.max_s, last.estimate_s) == ("processing", 1.0, None, 1.0)


def test_runs_of_the_same_plan_call_are_separate_jobs() -> None:
    first = polls(["queued", "running", "done"], start=1, at=0.0)
    second = polls(["queued", "done"], start=10, at=3600.0)
    third = polls(["queued", "queued"], start=20, at=7200.0)
    found = timelines([*third, *first, *second], state_path="$.status", terminal=TERMINAL)
    assert [(t.job, t.terminal) for t in found] == [
        ("submit", "done"),
        ("submit#2", "done"),
        ("submit#3", None),
    ]
    assert found[1].observations == [(0.0, "queued"), (1.0, "done")]
    assert found[1].states == [StateTiming(state="queued", min_s=0.0, max_s=1.0)]
    assert summarize(found).jobs == 3


def test_repeated_terminal_sightings_stay_in_one_job() -> None:
    (timeline,) = timelines(
        polls(["running", "done", "done"]), state_path="$.status", terminal=TERMINAL
    )
    assert timeline.terminal == "done"
    assert timeline.observations[-1] == (2.0, "done")


def test_jobs_are_grouped_by_their_poll_tag() -> None:
    found = timelines(
        [
            *polls(["queued", "done"], call="a"),
            *polls(["running", "failed"], start=5, call="b"),
        ],
        state_path="$.status",
        terminal=TERMINAL,
    )
    assert [(t.job, t.terminal) for t in found] == [("a", "done"), ("b", "failed")]


def test_unusable_exchanges_are_ignored() -> None:
    exchanges = [
        poll(1, None, 0.0),  # no state yet: still the origin
        poll(2, "queued", 1.0),
        poll(3, "queued", 1.5, tagged=False),
        poll(4, "done", 2.0, response=False),
        poll(5, "running", 3.0),
        poll(6, "done", 4.0),
    ]
    (timeline,) = timelines(exchanges, state_path="$.status", terminal=["done ", " failed"])
    assert timeline.observations == [(1.0, "queued"), (3.0, "running"), (4.0, "done")]
    assert timeline.terminal == "done"
    assert timelines([poll(1, None, 0.0)], state_path="$.status", terminal=TERMINAL) == [
        JobTimeline(job="submit", observations=[], states=[], terminal=None)
    ]
    assert timelines([], state_path="$.status", terminal=TERMINAL) == []


def test_numeric_states_are_compared_as_text() -> None:
    exchanges = [
        Exchange.model_validate(
            {
                **poll(i, None, float(i)).model_dump(by_alias=True),
                "response": {"status": 200, "body": {"kind": "json", "json": {"code": code}}},
            }
        )
        for i, code in enumerate((1, 1, 2, 3), start=1)
    ]
    (timeline,) = timelines(exchanges, state_path="$.code", terminal=["3"])
    assert [s.state for s in timeline.states] == ["1", "2"]
    assert timeline.terminal == "3"


def test_summary_fixed_and_empirical_durations() -> None:
    found = [
        *timelines(
            polls(["queued", "running", "running", "done"], call="a"),
            state_path="$.status",
            terminal=TERMINAL,
        ),
        *timelines(
            polls(["queued", "running", "done"], start=10, call="b"),
            state_path="$.status",
            terminal=TERMINAL,
        ),
        *timelines(
            polls(
                ["queued", "running", "running", "running", "running", "running", "failed"],
                start=20,
                call="c",
            ),
            state_path="$.status",
            terminal=TERMINAL,
        ),
        *timelines(
            polls(["queued", "running"], start=40, call="d"),
            state_path="$.status",
            terminal=TERMINAL,
        ),
    ]
    summary = summarize(found)
    assert summary.jobs == 4
    assert summary.states == ["queued", "running"]
    assert summary.durations["queued"] == DurationModel(kind="fixed", seconds=0.5)
    assert summary.durations["running"] == DurationModel(kind="empirical", samples=[2.0, 1.0, 5.0])
    assert summary.outcomes == [
        JobOutcome(state="done", probability=0.6667),
        JobOutcome(state="failed", probability=0.3333),
    ]
    assert math.isclose(sum(o.probability for o in summary.outcomes), 1.0, abs_tol=1e-9)
    assert summary.note.startswith("4 jobs observed; bounds come from polling")


def test_summary_single_sample_and_open_ended_states() -> None:
    (timeline,) = timelines(polls(["queued", "running"]), state_path="$.status", terminal=TERMINAL)
    summary = summarize([timeline])
    assert summary.states == ["queued", "running"]
    assert summary.durations == {"queued": DurationModel(kind="fixed", seconds=0.5)}
    assert summary.outcomes == []
    empty = summarize([])
    assert (empty.jobs, empty.states, empty.durations, empty.outcomes) == (0, [], {}, [])


def test_zero_length_states_are_fixed_at_zero() -> None:
    timeline = JobTimeline(
        job="x",
        observations=[],
        states=[StateTiming(state="instant", min_s=0.0, max_s=0.0)] * 2,
        terminal="done",
    )
    assert summarize([timeline]).durations["instant"] == DurationModel(kind="fixed", seconds=0.0)


@settings(max_examples=150, deadline=None)
@given(st.lists(st.integers(min_value=1, max_value=60), min_size=1, max_size=12))
def test_outcome_probabilities_always_form_a_valid_job_model(counts: list[int]) -> None:
    found = [
        JobTimeline.model_construct(job=f"{i}-{n}", observations=[], states=[], terminal=f"s{i}")
        for i, count in enumerate(counts)
        for n in range(count)
    ]
    outcomes = summarize(found).outcomes
    assert len(outcomes) == len(counts)
    assert abs(sum(o.probability for o in outcomes) - 1.0) <= 1e-6
    assert all(0.0 <= o.probability <= 1.0 for o in outcomes)
    shares = [counts[int(o.state[1:])] for o in outcomes]
    assert shares == sorted(shares, reverse=True)
    model = JobModel(
        name="job",
        collection="jobs",
        states=[
            JobStateModel(name="running", duration=DurationModel(kind="fixed", seconds=1.0)),
            *(JobStateModel(name=o.state, terminal=True) for o in outcomes),
        ],
        outcomes=outcomes,
    )
    assert model.outcomes == outcomes
